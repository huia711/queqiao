import Foundation

enum Translator {
    static let baseSystemPrompt = """
    你是专业的学术与技术翻译。把用户给出的每个编号段落翻译成简体中文。
    要求：
    - 忠实、通顺、简洁，符合中文表达习惯；
    - 模型名、数据集名、代码、公式、URL、人名保留原文；
    - 若段落由「·」分隔的多个图表短标签组成，译文同样用「·」分隔对应短语；
    - 文字来自屏幕文字识别，可能有断词、个别错字或缺失，请结合上下文理解后翻译，不要说明这些问题；
    - 不要解释，不要合并或拆分段落。
    输出格式：每段以 <<编号>> 单独起头，后面紧跟该段译文；按原编号顺序输出全部段落，不要输出其他内容。
    """

    /// Translate paragraphs in parallel streaming chunks; `onPartial` fires as tokens arrive.
    static func translate(
        _ items: [(Int, String)],
        model: String,
        onPartial: @escaping @MainActor (Int, String) -> Void,
        onFinal: @escaping @MainActor (Int, String, Bool) -> Void
    ) async {
        let chunks = makeChunks(items, budget: 1500)
        await withTaskGroup(of: [(Int, String, Bool)].self) { group in
            for chunk in chunks {
                group.addTask {
                    do {
                        return try await streamChunk(chunk, model: model, onPartial: onPartial)
                    } catch {
                        return chunk.map { ($0.0, "（翻译失败：\(error.localizedDescription)）", true) }
                    }
                }
            }
            for await results in group {
                for result in results { await onFinal(result.0, result.1, result.2) }
            }
        }
    }

    static func makeChunks(_ items: [(Int, String)], budget: Int) -> [[(Int, String)]] {
        var chunks: [[(Int, String)]] = []
        var current: [(Int, String)] = []
        var size = 0
        for item in items {
            if !current.isEmpty && size + item.1.count > budget {
                chunks.append(current)
                current = []
                size = 0
            }
            current.append(item)
            size += item.1.count
        }
        if !current.isEmpty { chunks.append(current) }
        return chunks
    }

    static func streamChunk(
        _ chunk: [(Int, String)],
        model: String,
        onPartial: @escaping @MainActor (Int, String) -> Void
    ) async throws -> [(Int, String, Bool)] {
        let prompt = chunk.enumerated().map { "<<\($0.offset + 1)>>\n\($0.element.1)" }.joined(separator: "\n\n")
        let combinedText = chunk.map(\.1).joined(separator: "\n")
        let system = baseSystemPrompt + Glossary.promptInstruction(for: combinedText)
        let content = try await streamComplete(system: system, user: prompt, model: model) { accumulated in
            let cleaned = clean(accumulated)
            let partialParts = parse(cleaned)
            if partialParts.isEmpty, chunk.count == 1, !cleaned.isEmpty {
                let stripped = cleaned.replacingOccurrences(of: "^<<\\s*1\\s*>>\\s*", with: "", options: .regularExpression)
                if !stripped.isEmpty {
                    await onPartial(chunk[0].0, stripped)
                }
                return
            }
            for (offset, item) in chunk.enumerated() {
                if let text = partialParts[offset + 1], !text.isEmpty {
                    await onPartial(item.0, text)
                }
            }
        }
        let parts = parse(content)
        var output: [(Int, String, Bool)] = []
        var missing: [(Int, String)] = []
        for (offset, item) in chunk.enumerated() {
            if let text = parts[offset + 1], !text.isEmpty {
                output.append((item.0, text, false))
            } else if chunk.count == 1, parts.isEmpty, !content.isEmpty {
                output.append((item.0, content, false))
            } else {
                missing.append(item)
            }
        }
        for item in missing {
            let singleSystem = baseSystemPrompt + Glossary.promptInstruction(for: item.1)
            let reply = try? await streamComplete(system: singleSystem, user: "<<1>>\n\(item.1)", model: model) { acc in
                let text = parse(clean(acc))[1] ?? clean(acc)
                if !text.isEmpty { await onPartial(item.0, text) }
            }
            let text = reply.map { parse($0)[1] ?? $0 }
            output.append((item.0, text ?? "（翻译失败）", text == nil))
        }
        return output
    }

    static func streamComplete(
        system: String,
        user: String,
        model: String,
        onDelta: @escaping (String) async -> Void
    ) async throws -> String {
        guard let url = URL(string: LensSettings.baseURL + "/chat/completions") else {
            throw LensError.badReply("接口地址无效")
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 180
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(LensSettings.apiKey)", forHTTPHeaderField: "Authorization")
        let body: [String: Any] = [
            "model": model,
            "temperature": 0.2,
            "stream": true,
            "messages": [
                ["role": "system", "content": system],
                ["role": "user", "content": user],
            ],
        ]
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (bytes, response) = try await URLSession.shared.bytes(for: request)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard code < 400 else {
            var errData = Data()
            for try await byte in bytes.prefix(240) { errData.append(byte) }
            throw LensError.http(code, String(decoding: errData, as: UTF8.self))
        }
        var accumulated = ""
        var lastEmit = Date.distantPast
        for try await rawLine in bytes.lines {
            let line = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
            guard line.hasPrefix("data:") else { continue }
            let payload = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
            if payload == "[DONE]" { break }
            guard let data = payload.data(using: .utf8),
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let choices = json["choices"] as? [[String: Any]]
            else { continue }
            let first = choices.first
            let delta = (first?["delta"] as? [String: Any])?["content"] as? String
                ?? (first?["message"] as? [String: Any])?["content"] as? String
            if let delta, !delta.isEmpty {
                accumulated += delta
                let now = Date()
                if now.timeIntervalSince(lastEmit) >= 0.045 {
                    lastEmit = now
                    await onDelta(accumulated)
                }
            }
        }
        let finalText = clean(accumulated)
        if finalText.isEmpty {
            return try await completeNonStream(system: system, user: user, model: model)
        }
        await onDelta(accumulated)
        return finalText
    }

    static func completeNonStream(system: String, user: String, model: String) async throws -> String {
        guard let url = URL(string: LensSettings.baseURL + "/chat/completions") else {
            throw LensError.badReply("接口地址无效")
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 180
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(LensSettings.apiKey)", forHTTPHeaderField: "Authorization")
        let body: [String: Any] = [
            "model": model,
            "temperature": 0.2,
            "stream": false,
            "messages": [
                ["role": "system", "content": system],
                ["role": "user", "content": user],
            ],
        ]
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await URLSession.shared.data(for: request)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard code < 400 else {
            throw LensError.http(code, String(decoding: data.prefix(200), as: UTF8.self))
        }
        guard
            let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
            let choices = json["choices"] as? [[String: Any]],
            let message = choices.first?["message"] as? [String: Any],
            let content = message["content"] as? String
        else {
            throw LensError.badReply(String(decoding: data.prefix(200), as: UTF8.self))
        }
        return clean(content)
    }

    static func clean(_ content: String) -> String {
        var text = content.replacingOccurrences(of: "(?s)<think>.*?</think>", with: "", options: .regularExpression)
        text = text.replacingOccurrences(of: "^\\s*```[a-zA-Z]*\\s*\\n|\\n?```\\s*$", with: "", options: .regularExpression)
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func parse(_ content: String) -> [Int: String] {
        guard let pattern = try? NSRegularExpression(pattern: "<<\\s*(\\d+)\\s*>>") else { return [:] }
        let source = content as NSString
        let matches = pattern.matches(in: content, range: NSRange(location: 0, length: source.length))
        var parts: [Int: String] = [:]
        for (index, match) in matches.enumerated() {
            guard let number = Int(source.substring(with: match.range(at: 1))) else { continue }
            let start = match.range.location + match.range.length
            let end = index + 1 < matches.count ? matches[index + 1].range.location : source.length
            let text = source.substring(with: NSRange(location: start, length: end - start))
            parts[number] = text.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return parts
    }
}
