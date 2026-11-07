import AppKit
import SwiftUI

@MainActor
final class OCRResult: ObservableObject {
    @Published var merged = ""
    @Published var lines = ""
    @Published var count = 0
    @Published var finished = false
    @Published var error: String?
    let started = Date()
    @Published var elapsed = ""

    func run(image: CGImage, axSnippets: [String]) async {
        do {
            let raw = try await Task.detached(priority: .userInitiated) { try OCR.recognize(image) }.value
            let text = TextLayout.recognizedText(from: raw, imageWidth: CGFloat(image.width), axSnippets: axSnippets)
            merged = text.merged
            lines = text.lines
            count = text.count
        } catch {
            self.error = error.localizedDescription
        }
        elapsed = String(format: "%.1f", Date().timeIntervalSince(started))
        finished = true
    }
}

final class OCRPanel: NSPanel {
    override var canBecomeKey: Bool { true }
}

/// Small floating panel next to the captured region with the recognized text, editable and copyable.
/// It is non-activating, so the app being read stays frontmost.
@MainActor
final class OCRPanelController {
    private var panel: OCRPanel?

    func show(_ result: OCRResult, near rect: CGRect, onTranslate: @escaping (String) -> Void) {
        close()
        let screen = NSScreen.screens.first { $0.frame.intersects(rect) } ?? NSScreen.main!
        let visible = screen.visibleFrame
        let size = CGSize(width: 440, height: min(max(rect.height + 110, 260), 520))
        var origin = CGPoint(x: rect.maxX + 14, y: rect.maxY - size.height)
        if origin.x + size.width > visible.maxX { origin.x = rect.minX - size.width - 14 }
        if origin.x < visible.minX {
            origin.x = min(max(rect.minX, visible.minX + 12), visible.maxX - size.width - 12)
            origin.y = rect.minY - size.height - 12
        }
        origin.y = min(max(origin.y, visible.minY + 12), visible.maxY - size.height - 12)

        let panel = OCRPanel(
            contentRect: NSRect(origin: origin, size: size),
            styleMask: [.titled, .closable, .resizable, .nonactivatingPanel, .utilityWindow],
            backing: .buffered,
            defer: false
        )
        panel.title = "鹊桥 OCR"
        panel.level = .floating
        panel.isReleasedWhenClosed = false
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.contentView = NSHostingView(rootView: OCRResultView(
            result: result,
            onTranslate: { [weak self] text in
                self?.close()
                onTranslate(text)
            },
            onClose: { [weak self] in self?.close() }
        ))
        panel.makeKeyAndOrderFront(nil)
        self.panel = panel
    }

    func close() {
        panel?.orderOut(nil)
        panel = nil
    }
}

struct OCRResultView: View {
    @ObservedObject var result: OCRResult
    let onTranslate: (String) -> Void
    let onClose: () -> Void
    @AppStorage("ocrKeepLines") private var keepLines = false
    @State private var text = ""
    @State private var note: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                if !result.finished { ProgressView().controlSize(.small) }
                Text(summary)
                    .font(.system(size: 11.5))
                    .foregroundStyle(result.error == nil ? Color.secondary : Color.red)
                    .lineLimit(1)
                Spacer()
                if let note {
                    Text(note).font(.system(size: 11.5)).foregroundStyle(lensAccent)
                }
                Picker("", selection: $keepLines) {
                    Text("合并段落").tag(false)
                    Text("保留换行").tag(true)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
                .disabled(!result.finished)
            }

            TextEditor(text: $text)
                .font(.system(size: 13.5))
                .scrollContentBackground(.hidden)
                .padding(6)
                .background(RoundedRectangle(cornerRadius: 7).fill(Color(nsColor: .textBackgroundColor)))
                .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(Color.secondary.opacity(0.25)))

            HStack(spacing: 8) {
                Text("可直接修改后再复制")
                    .font(.system(size: 11))
                    .foregroundStyle(.tertiary)
                Spacer()
                Button("关闭") { onClose() }
                    .keyboardShortcut(.cancelAction)
                Button("翻译") { onTranslate(text) }
                    .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                Button("复制") { copy() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(text.isEmpty)
            }
        }
        .padding(12)
        .frame(minWidth: 360, minHeight: 200)
        .onChange(of: result.finished) { _, finished in
            guard finished else { return }
            text = keepLines ? result.lines : result.merged
            if LensSettings.ocrAutoCopy, !text.isEmpty { copy(message: "已复制到剪贴板") }
        }
        .onChange(of: keepLines) { _, value in
            guard result.finished else { return }
            text = value ? result.lines : result.merged
            if LensSettings.ocrAutoCopy, !text.isEmpty { copy(message: "已重新复制") }
        }
    }

    private var summary: String {
        if let error = result.error { return error }
        if !result.finished { return "正在识别文字…" }
        if result.count == 0 { return "没有识别到文字" }
        return "识别到 \(result.count) 段 · \(text.count) 字 · \(result.elapsed) 秒"
    }

    private func copy(message: String = "已复制") {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        note = message
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { note = nil }
    }
}
