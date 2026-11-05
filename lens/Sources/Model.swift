import AppKit
import SwiftUI

let lensAccent = Color(red: 0.545, green: 0.18, blue: 0.235)
let lensAccentNS = NSColor(red: 0.545, green: 0.18, blue: 0.235, alpha: 1)

enum LensSettings {
    static let defaultBaseURL = "http://127.0.0.1:3425/v1"
    static let defaultAPIKey = "magpie"
    static let defaultModel = "cursor/claude-haiku-5-5@300k"
    /// Shown in the settings menu; screen translation needs fast, non-reasoning models.
    static let suggestedModels = [
        "cursor/claude-haiku-5-5@300k",
        "workbuddy/glm-5.3-flashx",
        "workbuddy/deepseek-v4.1-flash",
        "workbuddy/hy4std",
    ]

    static var baseURL: String {
        var value = stored("baseURL", defaultBaseURL)
        while value.hasSuffix("/") { value.removeLast() }
        return value
    }

    static var apiKey: String { stored("apiKey", defaultAPIKey) }
    static var model: String { stored("model", defaultModel) }

    private static func stored(_ key: String, _ fallback: String) -> String {
        let value = UserDefaults.standard.string(forKey: key)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return value.isEmpty ? fallback : value
    }
}

/// One OCR line, in image pixels with a top-left origin.
struct TextLine {
    var text: String
    var rect: CGRect
}

struct Paragraph: Identifiable {
    let id: Int
    var rect: CGRect
    var text: String
    var lineCount: Int
    var skip: Bool
    var translation: String?
    var failed = false
}

enum LensError: LocalizedError {
    case permission
    case noDisplay
    case http(Int, String)
    case badReply(String)

    var errorDescription: String? {
        switch self {
        case .permission: return "需要「屏幕录制」权限"
        case .noDisplay: return "找不到所选区域所在的屏幕"
        case let .http(code, body): return "翻译接口返回 HTTP \(code)：\(body)"
        case let .badReply(text): return "翻译接口返回的内容无法解析：\(text)"
        }
    }
}

@MainActor
final class LensSession: ObservableObject {
    let image: CGImage
    let scale: CGFloat
    let model = LensSettings.model
    let started = Date()
    @Published var paragraphs: [Paragraph] = []
    @Published var status = "正在识别文字…"
    @Published var error: String?
    @Published var finished = false
    @Published var elapsed = ""
    private(set) var crops: [Int: NSImage] = [:]

    init(image: CGImage, scale: CGFloat) {
        self.image = image
        self.scale = scale
    }

    var pointSize: CGSize {
        CGSize(width: CGFloat(image.width) / scale, height: CGFloat(image.height) / scale)
    }

    var translatable: [Paragraph] { paragraphs.filter { !$0.skip } }

    /// Translatable paragraphs from top to bottom: the numbering shared by margin cards and markers.
    var marginItems: [Paragraph] { translatable.sorted { $0.rect.minY < $1.rect.minY } }

    var summary: String {
        if let error { return error }
        if paragraphs.isEmpty { return status }
        let total = translatable.count
        let done = translatable.filter { $0.translation != nil }.count
        var text = "\(paragraphs.count) 段 · 已译 \(done)/\(total) · \(model)"
        if finished, !elapsed.isEmpty { text += " · \(elapsed) 秒" }
        return text
    }

    func pointRect(_ paragraph: Paragraph) -> CGRect {
        CGRect(
            x: paragraph.rect.minX / scale,
            y: paragraph.rect.minY / scale,
            width: paragraph.rect.width / scale,
            height: paragraph.rect.height / scale
        )
    }

    func crop(for paragraph: Paragraph) -> NSImage? { crops[paragraph.id] }

    func run() async {
        do {
            let image = self.image
            let lines = try await Task.detached(priority: .userInitiated) { try OCR.recognize(image) }.value
            let found = TextLayout.paragraphs(from: lines, imageWidth: CGFloat(image.width))
            for paragraph in found { crops[paragraph.id] = makeCrop(paragraph.rect) }
            paragraphs = found
            if translatable.isEmpty {
                status = found.isEmpty ? "没有识别到文字" : "没有需要翻译的外文"
                finish()
                return
            }
            status = "翻译中…"
            let items = translatable.map { ($0.id, $0.text) }
            await Translator.translate(items) { [weak self] id, text, failed in
                self?.apply(id: id, text: text, failed: failed)
            }
            status = "完成"
        } catch {
            self.error = error.localizedDescription
        }
        finish()
    }

    private func apply(id: Int, text: String, failed: Bool) {
        guard let index = paragraphs.firstIndex(where: { $0.id == id }) else { return }
        paragraphs[index].translation = text
        paragraphs[index].failed = failed
    }

    private func finish() {
        elapsed = String(format: "%.1f", Date().timeIntervalSince(started))
        finished = true
    }

    private func makeCrop(_ rect: CGRect) -> NSImage? {
        let pad = 5 * scale
        let bounds = CGRect(x: 0, y: 0, width: image.width, height: image.height)
        let area = rect.insetBy(dx: -pad, dy: -pad).intersection(bounds).integral
        guard !area.isEmpty, let cropped = image.cropping(to: area) else { return nil }
        return NSImage(cgImage: cropped, size: NSSize(width: area.width / scale, height: area.height / scale))
    }

    func copy(bilingual: Bool) {
        let blocks: [String] = paragraphs.compactMap { paragraph in
            if paragraph.skip { return bilingual ? paragraph.text : nil }
            let translation = paragraph.translation ?? ""
            return bilingual ? "\(paragraph.text)\n\(translation)" : translation
        }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(blocks.joined(separator: "\n\n"), forType: .string)
    }
}
