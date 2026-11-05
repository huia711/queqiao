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
    static var useGlossary: Bool { bool("useGlossary", true) }
    static var mergeDiagramLabels: Bool { bool("mergeDiagramLabels", true) }
    static var skipShortLabels: Bool { bool("skipShortLabels", false) }
    static var preferAccessibility: Bool { bool("preferAccessibility", true) }
    static var alwaysOnTop: Bool { bool("alwaysOnTop", true) }
    /// Default to in-place floating overlay right after box selection.
    static var defaultInPlaceOverlay: Bool { bool("defaultInPlaceOverlay", true) }

    /// Modifier preset: "cmd" (⌘), "opt_cmd" (⌥⌘), "ctrl_cmd" (⌃⌘), or "opt_shift" (⌥⇧).
    static var hotKeyModifiers: String { stored("hotKeyModifiers", "cmd") }
    static var captureKey: String { stored("captureKey", "O").uppercased() }
    static var selectionKey: String { stored("selectionKey", "T").uppercased() }
    static var recaptureKey: String { stored("recaptureKey", "R").uppercased() }

    static var modifierSymbol: String {
        switch hotKeyModifiers {
        case "cmd": return "⌘"
        case "opt_cmd": return "⌥⌘"
        case "ctrl_cmd": return "⌃⌘"
        default: return "⌥⇧"
        }
    }

    private static func stored(_ key: String, _ fallback: String) -> String {
        let value = UserDefaults.standard.string(forKey: key)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return value.isEmpty ? fallback : value
    }

    private static func bool(_ key: String, _ fallback: Bool) -> Bool {
        if UserDefaults.standard.object(forKey: key) == nil { return fallback }
        return UserDefaults.standard.bool(forKey: key)
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
    var skipReason: String? = nil
    var isDiagramGroup: Bool = false
    var translation: String?
    var streaming: Bool = false
    var cached: Bool = false
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
    let id = UUID().uuidString
    private(set) var image: CGImage?
    let scale: CGFloat
    var screenRect: CGRect?
    let isSelectionOnly: Bool
    private(set) var model = LensSettings.model
    private(set) var started = Date()
    private var rawLines: [TextLine] = []
    private var axSnippets: [String] = []
    @Published var paragraphs: [Paragraph] = []
    @Published var status = "正在识别文字…"
    @Published var error: String?
    @Published var finished = false
    @Published var elapsed = ""
    @Published var firstTokenTime = ""
    @Published var axMatchedCount = 0
    private(set) var crops: [Int: NSImage] = [:]
    var onRecapture: (() -> Void)?
    var onToggleOverlay: (() -> Void)?

    init(image: CGImage, scale: CGFloat, screenRect: CGRect? = nil, axSnippets: [String] = []) {
        self.image = image
        self.scale = scale
        self.screenRect = screenRect
        self.axSnippets = axSnippets
        self.isSelectionOnly = false
    }

    init(selectedText: String) {
        self.image = nil
        self.scale = 2
        self.screenRect = nil
        self.axSnippets = []
        self.isSelectionOnly = true
        let blocks = selectedText
            .replacingOccurrences(of: "\r\n", with: "\n")
            .components(separatedBy: "\n\n")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        let items = blocks.isEmpty ? [selectedText.trimmingCharacters(in: .whitespacesAndNewlines)] : blocks
        self.paragraphs = items.enumerated().map { index, block in
            // For explicit selection translation, allow both English->Chinese and Chinese->English.
            let letters = block.unicodeScalars.filter { CharacterSet.letters.contains($0) }
            return Paragraph(
                id: index,
                rect: CGRect(x: 0, y: CGFloat(index * 48), width: 600, height: 36),
                text: block,
                lineCount: block.components(separatedBy: .newlines).count,
                skip: letters.isEmpty,
                skipReason: letters.isEmpty ? "符号/数字" : nil
            )
        }
    }

    init(history: HistoryItem) {
        self.image = nil
        self.scale = 2
        self.screenRect = nil
        self.axSnippets = []
        self.isSelectionOnly = true
        self.model = history.model
        self.finished = true
        self.status = "历史记录"
        self.paragraphs = history.pairs.enumerated().map { index, pair in
            Paragraph(
                id: index,
                rect: CGRect(x: 0, y: CGFloat(index * 48), width: 600, height: 36),
                text: pair.original,
                lineCount: 1,
                skip: false,
                translation: pair.translation,
                cached: true
            )
        }
    }

    var pointSize: CGSize {
        guard let image else { return CGSize(width: 640, height: 420) }
        return CGSize(width: CGFloat(image.width) / scale, height: CGFloat(image.height) / scale)
    }

    var translatable: [Paragraph] { paragraphs.filter { !$0.skip } }

    /// Translatable paragraphs from top to bottom: the numbering shared by margin cards and markers.
    var marginItems: [Paragraph] { translatable.sorted { $0.rect.minY < $1.rect.minY } }

    var summary: String {
        if let error { return error }
        if paragraphs.isEmpty { return status }
        let total = translatable.count
        let done = translatable.filter { $0.translation != nil && !$0.streaming }.count
        let cachedCount = translatable.filter(\.cached).count
        var text = "\(paragraphs.count) 段 · 已译 \(done)/\(total) · \(model)"
        if cachedCount > 0 { text += " · 缓存 \(cachedCount)" }
        if axMatchedCount > 0 { text += " · 原生取词 \(axMatchedCount)" }
        if !finished, !firstTokenTime.isEmpty { text += " · 首字 \(firstTokenTime)s" }
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

    func updateCapture(image: CGImage, scale: CGFloat, screenRect: CGRect?, axSnippets: [String]) async {
        self.image = image
        self.screenRect = screenRect
        self.axSnippets = axSnippets
        self.model = LensSettings.model
        self.started = Date()
        self.finished = false
        self.error = nil
        self.elapsed = ""
        self.firstTokenTime = ""
        self.crops.removeAll()
        self.status = "正在识别文字…"
        await run()
    }

    func run() async {
        started = Date()
        finished = false
        error = nil
        do {
            if let image {
                let lines = try await Task.detached(priority: .userInitiated) { try OCR.recognize(image) }.value
                rawLines = lines
                buildParagraphsFromRawLines(image: image)
            }
            if translatable.isEmpty {
                status = paragraphs.isEmpty ? "没有识别到文字" : "没有需要翻译的外文"
                finish()
                return
            }
            await translateCurrentParagraphs()
        } catch {
            self.error = error.localizedDescription
            finish()
        }
    }

    func rebuildLayoutAndRetranslate() async {
        guard let image, !rawLines.isEmpty else { return }
        started = Date()
        finished = false
        firstTokenTime = ""
        buildParagraphsFromRawLines(image: image)
        await translateCurrentParagraphs()
    }

    private func buildParagraphsFromRawLines(image: CGImage) {
        let (found, axHits) = TextLayout.paragraphs(
            from: rawLines,
            imageWidth: CGFloat(image.width),
            mergeDiagramLabels: LensSettings.mergeDiagramLabels,
            skipShortLabels: LensSettings.skipShortLabels,
            axSnippets: LensSettings.preferAccessibility ? axSnippets : []
        )
        crops.removeAll()
        for paragraph in found { crops[paragraph.id] = makeCrop(paragraph.rect) }
        axMatchedCount = axHits
        paragraphs = found
    }

    private func translateCurrentParagraphs() async {
        let currentModel = model
        var uncached: [(Int, String)] = []
        for index in paragraphs.indices where !paragraphs[index].skip {
            let sourceText = paragraphs[index].text
            if let hit = TranslationCache.shared.get(model: currentModel, text: sourceText) {
                paragraphs[index].translation = hit
                paragraphs[index].cached = true
                paragraphs[index].streaming = false
                paragraphs[index].failed = false
            } else {
                uncached.append((paragraphs[index].id, sourceText))
            }
        }
        if uncached.isEmpty {
            status = "完成（全部命中缓存）"
            finish()
            HistoryStore.shared.record(session: self)
            return
        }
        status = "流式翻译中…"
        await Translator.translate(
            uncached,
            model: currentModel,
            onPartial: { [weak self] id, partial in
                self?.applyPartial(id: id, text: partial)
            },
            onFinal: { [weak self] id, final, failed in
                self?.applyFinal(id: id, text: final, failed: failed)
            }
        )
        status = "完成"
        finish()
        HistoryStore.shared.record(session: self)
    }

    private func applyPartial(id: Int, text: String) {
        guard let index = paragraphs.firstIndex(where: { $0.id == id }) else { return }
        if paragraphs[index].translation != nil, !paragraphs[index].streaming { return }
        if firstTokenTime.isEmpty {
            firstTokenTime = String(format: "%.1f", Date().timeIntervalSince(started))
        }
        paragraphs[index].translation = text
        paragraphs[index].streaming = true
    }

    private func applyFinal(id: Int, text: String, failed: Bool) {
        guard let index = paragraphs.firstIndex(where: { $0.id == id }) else { return }
        if firstTokenTime.isEmpty {
            firstTokenTime = String(format: "%.1f", Date().timeIntervalSince(started))
        }
        paragraphs[index].translation = text
        paragraphs[index].streaming = false
        paragraphs[index].failed = failed
        if !failed {
            TranslationCache.shared.put(model: model, text: paragraphs[index].text, translation: text)
        }
    }

    private func finish() {
        elapsed = String(format: "%.1f", Date().timeIntervalSince(started))
        finished = true
    }

    private func makeCrop(_ rect: CGRect) -> NSImage? {
        guard let image else { return nil }
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

    func markdown() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        var lines = [
            "# 鹊桥屏译记录",
            "",
            "- 时间：\(formatter.string(from: Date()))",
            "- 模型：`\(model)`",
            "",
        ]
        for paragraph in paragraphs {
            lines.append("> \(paragraph.text.replacingOccurrences(of: "\n", with: "\n> "))")
            lines.append("")
            if let translation = paragraph.translation, !paragraph.skip {
                lines.append(translation)
                lines.append("")
            }
        }
        return lines.joined(separator: "\n")
    }

    func exportMarkdownFile() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "queqiao-lens-\(Int(Date().timeIntervalSince1970)).md"
        panel.canCreateDirectories = true
        if panel.runModal() == .OK, let url = panel.url {
            try? markdown().write(to: url, atomically: true, encoding: .utf8)
        }
    }
}
