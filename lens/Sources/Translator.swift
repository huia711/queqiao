import Foundation

enum Translator {
    static let systemPrompt = """
    你是专业的学术与技术翻译。把用户给出的每个编号段落翻译成简体中文。
    要求：
    - 忠实、通顺、简洁，符合中文表达习惯；
    - 模型名、数据集名、代码、公式、URL、人名保留原文；
    - 文字来自屏幕文字识别，可能有断词、个别错字或缺失，请结合上下文理解后翻译，不要说明这些问题；
    - 不要解释，不要合并或拆分段落。
    输出格式：每段以 <<编号>> 单独起头，后面紧跟该段译文；按原编号顺序输出全部段落，不要输出其他内容。
    """

    /// Translate paragraphs in parallel chunks; `update` is called as each chunk finishes.
    static func translate(_ items: [(Int, String)], update: @escaping @MainActor (Int, String, Bool) -> Void) async {
        let chunks = makeChunks(items, budget: 1800)
        await withTaskGroup(of: [(Int, String, Bool)].self) { group in
            for chunk in chunks {
                group.addTask {
                    do {
                        return try await request(chunk)
                    } catch {
                        return chunk.map { ($0.0, "（翻译失败：\(error.localizedDescription)）", true) }
                    }
                }
            }
            for await results in group {
                for result in results { await update(result.0, result.1, result.2) }
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

    static func request(_ chunk: [(Int, String)]) async throws -> [(Int, String, Bool)] {
        let prompt = chunk.enumerated().map { "<<\($0.offset + 1)>>\n\($0.element.1)" }.joined(separator: "\n\n")
        let content = try await complete(user: prompt)
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
            let reply = try? await complete(user: "<<1>>\n\(item.1)")
            let text = reply.map { parse($0)[1] ?? $0 }
            output.append((item.0, text ?? "（翻译失败）", text == nil))
        }
        return output
    }

    static func complete(user: String) async throws -> String {
        guard let url = URL(string: LensSettings.baseURL + "/chat/completions") else {
            throw LensError.badReply("接口地址无效")
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 180
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(LensSettings.apiKey)", forHTTPHeaderField: "Authorization")
        let body: [String: Any] = [
            "model": LensSettings.model,
            "temperature": 0.2,
            "stream": false,
            "messages": [
                ["role": "system", "content": systemPrompt],
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
