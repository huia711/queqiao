import CoreGraphics
import Foundation
import Vision

enum OCR {
    static func recognize(_ image: CGImage) throws -> [TextLine] {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true
        if let custom = ProcessInfo.processInfo.environment["LENS_LANGS"] {
            request.recognitionLanguages = custom.split(separator: ",").map(String.init)
        } else {
            request.recognitionLanguages = ["en-US", "zh-Hans"]
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
                y: (1 - box.maxY) * height,
                width: box.width * width,
                height: box.height * height
            )
            return TextLine(text: candidate.string, rect: rect)
        }
    }
}

/// Turns OCR lines into paragraphs in reading order.
enum TextLayout {
    static func paragraphs(from raw: [TextLine], imageWidth: CGFloat) -> [Paragraph] {
        let lines = mergeRows(raw.filter { !$0.text.trimmingCharacters(in: .whitespaces).isEmpty })
        let groups = group(lines)
        var paragraphs: [Paragraph] = []
        for (index, group) in groups.enumerated() {
            let rect = group.dropFirst().reduce(group[0].rect) { $0.union($1.rect) }
            let text = join(group)
            paragraphs.append(
                Paragraph(id: index, rect: rect, text: text, lineCount: group.count, skip: !needsTranslation(text))
            )
        }
        return order(paragraphs, width: imageWidth)
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
                if gap < -0.4 * height || gap > 0.9 * height { continue }
                let ratio = rect.height / max(height, 1)
                if ratio < 0.68 || ratio > 1.6 { continue }
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
        let letters = text.unicodeScalars.filter { CharacterSet.letters.contains($0) }
        if letters.count < 3 { return false }
        let chinese = letters.filter { (0x4E00...0x9FFF).contains($0.value) || (0x3400...0x4DBF).contains($0.value) }
        return Double(chinese.count) / Double(letters.count) < 0.4
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
