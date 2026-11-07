import CoreGraphics
import Foundation
import Vision

enum OCR {
    /// Vision's accurate recognizer sometimes drops a whole text block (e.g. a paragraph right under a large
    /// heading). Run it on the full image and on overlapping horizontal strips, then keep every line that
    /// is not already covered, so nothing visible disappears from the result.
    static func recognize(_ image: CGImage) throws -> [TextLine] {
        var lines = try recognizeRegion(image, offsetY: 0)
        let height = image.height
        let strip = 360
        if height > strip {
            var top = 0
            while top < height {
                let stripHeight = min(strip, height - top)
                if let piece = image.cropping(to: CGRect(x: 0, y: top, width: image.width, height: stripHeight)) {
                    let found = try recognizeRegion(piece, offsetY: CGFloat(top))
                    lines = merge(lines, found)
                }
                if top + stripHeight >= height { break }
                top += strip * 2 / 3
            }
        }
        if !lines.contains(where: { containsCJK($0.text) }) {
            lines = merge(lines, try recognizeRegion(image, offsetY: 0, level: .fast))
        }
        return lines.map { line in
            containsCJK(line.text) ? line : TextLine(text: asciiPunctuation(line.text), rect: line.rect)
        }
    }

    static func containsCJK(_ text: String) -> Bool {
        text.unicodeScalars.contains { scalar in
            (0x4E00...0x9FFF).contains(scalar.value) || (0x3400...0x4DBF).contains(scalar.value)
                || (0x3040...0x30FF).contains(scalar.value) || (0xAC00...0xD7AF).contains(scalar.value)
        }
    }

    private static func asciiPunctuation(_ text: String) -> String {
        let table: [Character: String] = [
            "（": "(", "）": ")", "，": ", ", "：": ": ", "；": "; ", "！": "! ", "？": "? ",
            "％": "%", "。": ". ", "“": "\"", "”": "\"", "‘": "'", "’": "'",
        ]
        var output = ""
        for character in text { output += table[character] ?? String(character) }
        return output.replacingOccurrences(of: "  ", with: " ").trimmingCharacters(in: .whitespaces)
    }

    /// Adds lines from `extra` whose box does not substantially overlap a line already in `base`.
    static func merge(_ base: [TextLine], _ extra: [TextLine]) -> [TextLine] {
        var result = base
        for line in extra {
            let covered = result.contains { existing in
                let overlap = existing.rect.intersection(line.rect)
                guard !overlap.isNull else { return false }
                let area = overlap.width * overlap.height
                return area > 0.35 * min(existing.rect.width * existing.rect.height, line.rect.width * line.rect.height)
            }
            if !covered { result.append(line) }
        }
        return result
    }

    private static func recognizeRegion(
        _ image: CGImage,
        offsetY: CGFloat,
        level: VNRequestTextRecognitionLevel = .accurate
    ) throws -> [TextLine] {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = level
        request.usesLanguageCorrection = true
        if let custom = ProcessInfo.processInfo.environment["LENS_LANGS"] {
            request.recognitionLanguages = custom.split(separator: ",").map(String.init)
        } else if level == .fast {
            request.recognitionLanguages = ["en-US"]
        } else {
            request.recognitionLanguages = ["zh-Hans", "en-US"]
        }
        let handler = VNImageRequestHandler(cgImage: image, options: [:])
        try handler.perform([request])
        let width = CGFloat(image.width)
        let height = CGFloat(image.height)
        return (request.results ?? []).compactMap { observation in
            guard let candidate = observation.topCandidates(1).first else { return nil }
            let box = observation.boundingBox
            let rect = CGRect(
                x: box.minX * width,
                y: (1 - box.maxY) * height + offsetY,
                width: box.width * width,
                height: box.height * height
            )
            return TextLine(text: candidate.string, rect: rect)
        }
    }
}

/// Turns OCR lines into paragraphs in reading order.
enum TextLayout {
    struct Classification {
        var skip: Bool
        var reason: String?
    }

    static func paragraphs(
        from raw: [TextLine],
        imageWidth: CGFloat,
        mergeDiagramLabels: Bool = true,
        skipShortLabels: Bool = false,
        axSnippets: [String] = [],
        dropEdgeFragments: Bool = true
    ) -> ([Paragraph], Int) {
        let lines = mergeRows(raw.filter { !$0.text.trimmingCharacters(in: .whitespaces).isEmpty })
        var kept = lines
        if dropEdgeFragments {
            let heights = lines.map(\.rect.height).sorted()
            let median = heights.isEmpty ? 0 : heights[heights.count / 2]
            let trimmed = lines.filter { !isCutAtEdge($0, imageWidth: imageWidth, medianHeight: median) }
            if !trimmed.isEmpty { kept = trimmed }
        }
        let groups = group(kept)
        var paragraphs: [Paragraph] = []
        var axHits = 0
        for (index, group) in groups.enumerated() {
            let rect = group.dropFirst().reduce(group[0].rect) { $0.union($1.rect) }
            var text = join(group)
            if let matched = matchAccessibilityText(ocrText: text, axSnippets: axSnippets) {
                text = matched
                axHits += 1
            }
            let cls = classify(text)
            var paragraph = Paragraph(
                id: index,
                rect: rect,
                text: text,
                lineCount: group.count,
                skip: cls.skip,
                skipReason: cls.reason
            )
            paragraph.lineTexts = group.map { $0.text.trimmingCharacters(in: .whitespaces) }
            paragraphs.append(paragraph)
        }
        var ordered = order(paragraphs, width: imageWidth)
        if skipShortLabels {
            for idx in ordered.indices where !ordered[idx].skip && isShortDiagramLabel(ordered[idx], imageWidth: imageWidth) {
                ordered[idx].skip = true
                ordered[idx].skipReason = "图内标签"
            }
        } else if mergeDiagramLabels {
            ordered = clusterDiagramLabels(ordered, imageWidth: imageWidth)
        }
        return (ordered, axHits)
    }

    /// Plain text of a capture for OCR: every paragraph (including Chinese and labels), in reading order.
    static func recognizedText(from raw: [TextLine], imageWidth: CGFloat, axSnippets: [String] = []) -> (merged: String, lines: String, count: Int) {
        let (found, _) = paragraphs(
            from: raw,
            imageWidth: imageWidth,
            mergeDiagramLabels: false,
            skipShortLabels: false,
            axSnippets: axSnippets,
            dropEdgeFragments: false
        )
        let merged = found.map(\.text).joined(separator: "\n\n")
        let lines = found.map { $0.lineTexts.isEmpty ? $0.text : $0.lineTexts.joined(separator: "\n") }.joined(separator: "\n\n")
        return (merged, lines, found.count)
    }

    /// A short, clipped-looking line touching the top edge of the capture is usually a sliver of UI cut by
    /// the selection rectangle (a tab title, a toolbar label); translating it only adds noise. A real line
    /// that the user selected tightly keeps its full height, so it is not dropped.
    static func isCutAtEdge(_ line: TextLine, imageWidth: CGFloat, medianHeight: CGFloat) -> Bool {
        guard line.rect.minY <= 2, medianHeight > 0, line.rect.height < 0.75 * medianHeight else { return false }
        let words = line.text.split(whereSeparator: \.isWhitespace).count
        return words <= 4 && line.rect.width < 0.3 * imageWidth
    }

    static func isShortDiagramLabel(_ paragraph: Paragraph, imageWidth: CGFloat) -> Bool {
        guard paragraph.lineCount <= 2, paragraph.rect.width < 0.42 * imageWidth else { return false }
        let trimmed = paragraph.text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.count > 44 { return false }
        if trimmed.hasPrefix("Figure ") || trimmed.hasPrefix("Table ") { return false }
        if let first = trimmed.first, first.isNumber, trimmed.contains(" ") {
            let token = trimmed.split(separator: " ").first ?? ""
            if token.allSatisfy({ $0.isNumber || $0 == "." }) { return false }
        }
        let words = trimmed.split(whereSeparator: \.isWhitespace)
        return words.count <= 5 && !trimmed.hasSuffix(".")
    }

    /// Combines nearby short diagram labels inside the same figure band into one compact paragraph.
    static func clusterDiagramLabels(_ items: [Paragraph], imageWidth: CGFloat) -> [Paragraph] {
        var output: [Paragraph] = []
        var pending: [Paragraph] = []

        func flushPending() {
            guard !pending.isEmpty else { return }
            if pending.count == 1 {
                output.append(pending[0])
            } else {
                let sorted = pending.sorted {
                    abs($0.rect.minY - $1.rect.minY) < 18 ? $0.rect.minX < $1.rect.minX : $0.rect.minY < $1.rect.minY
                }
                let unionRect = sorted.dropFirst().reduce(sorted[0].rect) { $0.union($1.rect) }
                let combinedText = sorted.map(\.text).joined(separator: " · ")
                output.append(
                    Paragraph(
                        id: sorted[0].id,
                        rect: unionRect,
                        text: combinedText,
                        lineCount: sorted.count,
                        skip: false,
                        isDiagramGroup: true
                    )
                )
            }
            pending.removeAll()
        }

        for item in items {
            if !item.skip && isShortDiagramLabel(item, imageWidth: imageWidth) {
                if let last = pending.last {
                    let currentBand = pending.dropFirst().reduce(pending[0].rect) { $0.union($1.rect) }
                    let verticalGap = item.rect.minY - currentBand.maxY
                    if verticalGap > max(120, currentBand.height * 1.4) || pending.count >= 8 {
                        flushPending()
                    } else {
                        _ = last
                    }
                }
                pending.append(item)
            } else {
                flushPending()
                output.append(item)
            }
        }
        flushPending()
        return output
    }

    /// Replaces noisy OCR text with an exact Accessibility string when token overlap is high.
    static func matchAccessibilityText(ocrText: String, axSnippets: [String]) -> String? {
        guard !axSnippets.isEmpty, ocrText.count >= 12 else { return nil }
        let ocrWords = tokenSet(ocrText)
        guard ocrWords.count >= 3 else { return nil }
        var best: (score: Double, text: String)?
        for candidate in axSnippets {
            let candWords = tokenSet(candidate)
            guard !candWords.isEmpty else { continue }
            let common = ocrWords.intersection(candWords).count
            let score = Double(common) / Double(max(ocrWords.count, candWords.count))
            let lengthRatio = Double(min(ocrText.count, candidate.count)) / Double(max(ocrText.count, candidate.count))
            if score >= 0.62, lengthRatio >= 0.65 {
                if best == nil || score > best!.score {
                    best = (score, candidate)
                }
            }
        }
        return best?.text
    }

    private static func tokenSet(_ text: String) -> Set<String> {
        let parts = text.lowercased().split { !$0.isLetter && !$0.isNumber }
        return Set(parts.filter { $0.count >= 2 }.map(String.init))
    }

    /// Vision sometimes splits one visual line into several fragments; glue fragments on the same row.
    static func mergeRows(_ lines: [TextLine]) -> [TextLine] {
        var result: [TextLine] = []
        for line in lines.sorted(by: { $0.rect.minX < $1.rect.minX }) {
            let height = line.rect.height
            if let index = result.firstIndex(where: { candidate in
                let overlap = min(candidate.rect.maxY, line.rect.maxY) - max(candidate.rect.minY, line.rect.minY)
                let gap = line.rect.minX - candidate.rect.maxX
                return overlap > 0.6 * min(candidate.rect.height, height) && gap > -0.5 * height && gap < 0.9 * height
            }) {
                result[index].text += " " + line.text
                result[index].rect = result[index].rect.union(line.rect)
            } else {
                result.append(line)
            }
        }
        return result
    }

    static func group(_ lines: [TextLine]) -> [[TextLine]] {
        var groups: [[TextLine]] = []
        var bounds: [CGRect] = []
        func typical(_ group: [TextLine]) -> CGFloat {
            let heights = group.map(\.rect.height).sorted()
            return heights[heights.count / 2]
        }
        for line in lines.sorted(by: { $0.rect.minY < $1.rect.minY }) {
            let rect = line.rect
            var best = -1
            var bestGap = CGFloat.greatestFiniteMagnitude
            for (index, group) in groups.enumerated() {
                let last = group[group.count - 1].rect
                let height = typical(group)
                let gap = rect.minY - last.maxY
                if gap < -0.4 * height || gap > 0.75 * height { continue }
                // A paragraph break is a gap clearly larger than the line gaps already inside the group.
                if group.count >= 2 {
                    let inner = zip(group, group.dropFirst()).map { $1.rect.minY - $0.rect.maxY }.max() ?? 0
                    if gap > max(inner * 2.2, inner + 0.35 * height) { continue }
                } else if gap > 0.55 * height {
                    continue
                }
                let ratio = rect.height / max(height, 1)
                let cjkGroup = group.contains { OCR.containsCJK($0.text) }
                if ratio < (cjkGroup ? 0.6 : 0.68) || ratio > 1.6 { continue }
                let block = bounds[index]
                let overlap = min(rect.maxX, block.maxX) - max(rect.minX, block.minX)
                if overlap < 0.3 * min(rect.width, block.width) { continue }
                if rect.minX - block.minX > 1.2 * height { continue }
                let right = max(block.maxX, rect.maxX)
                let shortfall = right - last.maxX
                if shortfall > max(3.5 * height, 0.22 * (right - block.minX)) { continue }
                if gap < bestGap {
                    best = index
                    bestGap = gap
                }
            }
            if best >= 0 {
                groups[best].append(line)
                bounds[best] = bounds[best].union(rect)
            } else {
                groups.append([line])
                bounds.append(rect)
            }
        }
        return groups
    }

    static func join(_ lines: [TextLine]) -> String {
        var output = ""
        for line in lines {
            let text = line.text.trimmingCharacters(in: .whitespaces)
            if output.isEmpty {
                output = text
            } else if output.hasSuffix("-"), let first = text.first, first.isLowercase {
                output.removeLast()
                output += text
            } else if isCJK(output.last) || isCJK(text.first) {
                output += text
            } else {
                output += " " + text
            }
        }
        return output
    }

    static func isCJK(_ character: Character?) -> Bool {
        guard let scalar = character?.unicodeScalars.first else { return false }
        return (0x4E00...0x9FFF).contains(scalar.value) || (0x3400...0x4DBF).contains(scalar.value)
            || (0x3000...0x303F).contains(scalar.value) || (0xFF00...0xFFEF).contains(scalar.value)
    }

    /// Skip text that is already mostly Chinese, or has almost no letters (numbers, symbols).
    static func needsTranslation(_ text: String) -> Bool {
        !classify(text).skip
    }

    static func classify(_ text: String) -> Classification {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let letters = text.unicodeScalars.filter { CharacterSet.letters.contains($0) }
        if letters.count < 3 { return Classification(skip: true, reason: "符号/数字") }
        let chinese = letters.filter { (0x4E00...0x9FFF).contains($0.value) || (0x3400...0x4DBF).contains($0.value) }
        if Double(chinese.count) / Double(letters.count) >= 0.4 {
            return Classification(skip: true, reason: "中文")
        }
        if isLikelyFormulaOrCode(trimmed) {
            return Classification(skip: true, reason: "公式/代码")
        }
        return Classification(skip: false, reason: nil)
    }

    /// Detects standalone mathematical equations or source-code snippets so they stay untranslated.
    static func isLikelyFormulaOrCode(_ text: String) -> Bool {
        let nonSpace = text.unicodeScalars.filter { !CharacterSet.whitespacesAndNewlines.contains($0) }
        guard nonSpace.count >= 6 else { return false }
        let mathSymbols = CharacterSet(charactersIn: "=+−*/<>≤≥≠≈∑∏∫√∂∇∈∉∪∩⊂⊆×÷±→←↔⇒⇔{}[]^_|\\")
        let mathCount = nonSpace.filter { mathSymbols.contains($0) }.count
        let proseWords = text.split(whereSeparator: \.isWhitespace).filter { word in
            word.count >= 4 && word.allSatisfy(\.isLetter)
        }
        if Double(mathCount) / Double(nonSpace.count) > 0.22 && proseWords.count < 5 {
            return true
        }
        let codePrefixes = ["def ", "class ", "import ", "from ", "return ", "func ", "let ", "var ", "const ", "#include", "SELECT ", "git ", "sudo ", "curl "]
        if codePrefixes.contains(where: { text.hasPrefix($0) }) && proseWords.count < 8 {
            return true
        }
        let braces = nonSpace.filter { "{};=()".unicodeScalars.contains($0) }.count
        if Double(braces) / Double(nonSpace.count) > 0.18 && proseWords.count < 6 {
            return true
        }
        return false
    }

    /// Top-to-bottom, but within a band of narrow paragraphs read the left column before the right one.
    static func order(_ paragraphs: [Paragraph], width: CGFloat) -> [Paragraph] {
        func column(_ paragraph: Paragraph) -> Int { paragraph.rect.midX < width / 2 ? 0 : 1 }
        var result: [Paragraph] = []
        var band: [Paragraph] = []
        func flush() {
            result += band.sorted { (column($0), $0.rect.minY) < (column($1), $1.rect.minY) }
            band = []
        }
        for paragraph in paragraphs.sorted(by: { $0.rect.minY < $1.rect.minY }) {
            if paragraph.rect.width > 0.6 * width {
                flush()
                result.append(paragraph)
            } else {
                band.append(paragraph)
            }
        }
        flush()
        return result
    }
}
