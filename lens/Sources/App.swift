import AppKit
import Carbon.HIToolbox
import ServiceManagement
import SwiftUI

@main
struct LensMain {
    static func main() {
        if let index = CommandLine.arguments.firstIndex(of: "--render") {
            Offline.run(Array(CommandLine.arguments[(index + 1)...]))
            return
        }
        if let index = CommandLine.arguments.firstIndex(of: "--ocr"), index + 1 < CommandLine.arguments.count {
            Offline.ocr(CommandLine.arguments[index + 1])
            return
        }
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var statusItem: NSStatusItem!
    private let selection = SelectionController()
    private var hotKeys: [EventHotKeyRef?] = []
    private var hotKeyHandlerInstalled = false
    private var resultWindows: [NSWindow] = []
    private var settingsWindow: NSWindow?
    private var lastCaptureRect: CGRect?
    private weak var lastCaptureSession: LensSession?
    private let overlayController = InPlaceOverlayController()
    private let ocrController = OCRPanelController()
    private var historyMenu = NSMenu()

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "character.book.closed", accessibilityDescription: "鹊桥屏译")
            button.image?.isTemplate = true
        }
        rebuildStatusMenu()
        registerHotKeys()
        if !CGPreflightScreenCaptureAccess() { CGRequestScreenCaptureAccess() }
    }

    func rebuildStatusMenu() {
        let menu = NSMenu()
        menu.delegate = self
        let mod = LensSettings.modifierSymbol
        menu.addItem(withTitle: "框选翻译（\(mod)\(LensSettings.captureKey)）", action: #selector(startSelection), keyEquivalent: "").target = self
        menu.addItem(withTitle: "划词翻译（\(mod)\(LensSettings.selectionKey)）", action: #selector(translateSelectedTextFromMenu), keyEquivalent: "").target = self
        let recapItem = menu.addItem(withTitle: "重截上次区域（\(mod)\(LensSettings.recaptureKey)）", action: #selector(recaptureLastArea), keyEquivalent: "")
        recapItem.target = self
        menu.addItem(withTitle: "框选识别文字 OCR（\(LensSettings.ocrModifierSymbol)\(LensSettings.ocrKey)）", action: #selector(startOCR), keyEquivalent: "").target = self
        menu.addItem(.separator())
        let historyParent = NSMenuItem(title: "最近翻译历史", action: nil, keyEquivalent: "")
        historyParent.submenu = historyMenu
        menu.addItem(historyParent)
        menu.addItem(.separator())
        menu.addItem(withTitle: "设置…", action: #selector(openSettings), keyEquivalent: ",").target = self
        menu.addItem(withTitle: "退出鹊桥屏译", action: #selector(quit), keyEquivalent: "q").target = self
        statusItem.menu = menu
    }

    func menuWillOpen(_ menu: NSMenu) {
        historyMenu.removeAllItems()
        let items = HistoryStore.shared.items
        if items.isEmpty {
            let empty = NSMenuItem(title: "暂无历史记录", action: nil, keyEquivalent: "")
            empty.isEnabled = false
            historyMenu.addItem(empty)
        } else {
            for (idx, item) in items.prefix(12).enumerated() {
                let mItem = NSMenuItem(
                    title: "[\(item.sourceKind)] \(item.title) · \(item.relativeTime)",
                    action: #selector(openHistoryItem(_:)),
                    keyEquivalent: ""
                )
                mItem.tag = idx
                mItem.target = self
                historyMenu.addItem(mItem)
            }
            historyMenu.addItem(.separator())
            let clearItem = NSMenuItem(title: "清空历史记录", action: #selector(clearHistory), keyEquivalent: "")
            clearItem.target = self
            historyMenu.addItem(clearItem)
        }
    }

    @objc private func openHistoryItem(_ sender: NSMenuItem) {
        let items = HistoryStore.shared.items
        guard sender.tag >= 0, sender.tag < items.count else { return }
        let session = LensSession(history: items[sender.tag])
        show(session, near: nil)
    }

    @objc private func clearHistory() {
        HistoryStore.shared.clear()
    }

    func registerHotKeys() {
        for ref in hotKeys {
            if let ref { UnregisterEventHotKey(ref) }
        }
        hotKeys.removeAll()

        if !hotKeyHandlerInstalled {
            var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
            let pointer = Unmanaged.passUnretained(self).toOpaque()
            InstallEventHandler(GetApplicationEventTarget(), { _, event, user in
                guard let user, let event else { return noErr }
                var hkID = EventHotKeyID()
                GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil, MemoryLayout<EventHotKeyID>.size, nil, &hkID)
                let delegate = Unmanaged<AppDelegate>.fromOpaque(user).takeUnretainedValue()
                DispatchQueue.main.async {
                    switch hkID.id {
                    case 1: delegate.startSelection()
                    case 2: delegate.translateSelectedText()
                    case 3: delegate.recaptureLastArea()
                    case 4: delegate.startOCR()
                    default: break
                    }
                }
                return noErr
            }, 1, &spec, pointer, nil)
            hotKeyHandlerInstalled = true
        }

        let bindings: [(UInt32, String)] = [
            (1, LensSettings.captureKey),
            (2, LensSettings.selectionKey),
            (3, LensSettings.recaptureKey),
        ]
        let carbonMods = Self.carbonModifierFlags()
        for (idNum, letter) in bindings {
            guard let code = Self.keyCode(for: letter) else { continue }
            var ref: EventHotKeyRef?
            let hkID = EventHotKeyID(signature: OSType(0x5151_4C53), id: idNum)
            RegisterEventHotKey(code, carbonMods, hkID, GetApplicationEventTarget(), 0, &ref)
            hotKeys.append(ref)
        }
        if let code = Self.keyCode(for: LensSettings.ocrKey) {
            let extra = LensSettings.hotKeyModifiers == "opt_shift" ? cmdKey : shiftKey
            var ref: EventHotKeyRef?
            let hkID = EventHotKeyID(signature: OSType(0x5151_4C53), id: 4)
            RegisterEventHotKey(code, carbonMods | UInt32(extra), hkID, GetApplicationEventTarget(), 0, &ref)
            hotKeys.append(ref)
        }
    }

    private static func carbonModifierFlags() -> UInt32 {
        switch LensSettings.hotKeyModifiers {
        case "cmd": return UInt32(cmdKey)
        case "opt_cmd": return UInt32(optionKey | cmdKey)
        case "ctrl_cmd": return UInt32(controlKey | cmdKey)
        default: return UInt32(optionKey | shiftKey)
        }
    }

    private static func keyCode(for letter: String) -> UInt32? {
        let map: [String: Int] = [
            "A": kVK_ANSI_A, "B": kVK_ANSI_B, "C": kVK_ANSI_C, "D": kVK_ANSI_D,
            "E": kVK_ANSI_E, "F": kVK_ANSI_F, "G": kVK_ANSI_G, "H": kVK_ANSI_H,
            "I": kVK_ANSI_I, "J": kVK_ANSI_J, "K": kVK_ANSI_K, "L": kVK_ANSI_L,
            "M": kVK_ANSI_M, "N": kVK_ANSI_N, "O": kVK_ANSI_O, "P": kVK_ANSI_P,
            "Q": kVK_ANSI_Q, "R": kVK_ANSI_R, "S": kVK_ANSI_S, "T": kVK_ANSI_T,
            "U": kVK_ANSI_U, "V": kVK_ANSI_V, "W": kVK_ANSI_W, "X": kVK_ANSI_X,
            "Y": kVK_ANSI_Y, "Z": kVK_ANSI_Z,
        ]
        return map[letter.uppercased()].map(UInt32.init)
    }

    @objc func startSelection() {
        selection.begin { [weak self] rect in
            guard let self, let rect else { return }
            Task { @MainActor in await self.translate(rect) }
        }
    }

    @objc func startOCR() {
        selection.begin(hint: "拖动框选要识别文字的区域 · Esc 取消") { [weak self] rect in
            guard let self, let rect else { return }
            Task { @MainActor in await self.recognize(rect) }
        }
    }

    @MainActor
    private func recognize(_ rect: CGRect) async {
        let axSnippets = LensSettings.preferAccessibility ? AccessibilityReader.sampleStrings(in: rect) : []
        do {
            let (image, _) = try await ScreenCapture.capture(rect)
            let result = OCRResult()
            ocrController.show(result, near: rect) { [weak self] text in
                guard let self else { return }
                let session = LensSession(selectedText: text)
                self.show(session, near: rect)
                Task { await session.run() }
            }
            await result.run(image: image, axSnippets: axSnippets)
        } catch LensError.permission {
            permissionAlert()
        } catch {
            let alert = NSAlert()
            alert.messageText = "截取屏幕失败"
            alert.informativeText = error.localizedDescription
            alert.runModal()
        }
    }

    @objc func recaptureLastArea() {
        guard let rect = lastCaptureRect else {
            startSelection()
            return
        }
        Task { @MainActor in
            await self.translate(rect, reuseSession: self.lastCaptureSession)
        }
    }

    @objc func translateSelectedText() {
        runSelectionTranslation(triggeredByHotKey: true)
    }

    @objc func translateSelectedTextFromMenu() {
        runSelectionTranslation(triggeredByHotKey: false)
    }

    private func runSelectionTranslation(triggeredByHotKey: Bool) {
        Task { @MainActor in
            guard let text = await AccessibilityReader.readSelectedText(), !text.isEmpty else {
                // If triggered by ⌘T and nothing is selected, forward ⌘T to the active app (e.g. New Tab in browser).
                if triggeredByHotKey, let code = Self.keyCode(for: LensSettings.selectionKey) {
                    forwardShortcutToFrontApp(keyCode: CGKeyCode(code))
                    return
                }
                let alert = NSAlert()
                alert.messageText = "未读取到选中的文字"
                alert.informativeText = "请先在当前应用里选中一段文字再按 \(LensSettings.modifierSymbol)\(LensSettings.selectionKey)。若首次使用，请在「系统设置 → 隐私与安全性 → 辅助功能」里允许「鹊桥屏译」。"
                alert.addButton(withTitle: "打开辅助功能设置")
                alert.addButton(withTitle: "好")
                if alert.runModal() == .alertFirstButtonReturn {
                    _ = AccessibilityReader.isTrusted(prompt: true)
                }
                return
            }
            let session = LensSession(selectedText: text)
            show(session, near: nil)
            await session.run()
        }
    }

    private func forwardShortcutToFrontApp(keyCode: CGKeyCode) {
        for ref in hotKeys {
            if let ref { UnregisterEventHotKey(ref) }
        }
        hotKeys.removeAll()
        if let src = CGEventSource(stateID: .privateState),
           let down = CGEvent(keyboardEventSource: src, virtualKey: keyCode, keyDown: true),
           let up = CGEvent(keyboardEventSource: src, virtualKey: keyCode, keyDown: false) {
            let flags: CGEventFlags
            switch LensSettings.hotKeyModifiers {
            case "cmd": flags = .maskCommand
            case "opt_cmd": flags = [.maskCommand, .maskAlternate]
            case "ctrl_cmd": flags = [.maskCommand, .maskControl]
            default: flags = [.maskAlternate, .maskShift]
            }
            down.flags = flags
            up.flags = flags
            down.post(tap: .cghidEventTap)
            up.post(tap: .cghidEventTap)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { [weak self] in
            self?.registerHotKeys()
        }
    }

    @MainActor
    private func translate(_ rect: CGRect, reuseSession: LensSession? = nil) async {
        lastCaptureRect = rect
        let axSnippets = LensSettings.preferAccessibility ? AccessibilityReader.sampleStrings(in: rect) : []
        do {
            let (image, scale) = try await ScreenCapture.capture(rect)
            if let existing = reuseSession {
                await existing.updateCapture(image: image, scale: scale, screenRect: rect, axSnippets: axSnippets)
                return
            }
            let session = LensSession(image: image, scale: scale, screenRect: rect, axSnippets: axSnippets)
            session.onRecapture = { [weak self, weak session] in
                guard let self, let rect = session?.screenRect else { return }
                Task { @MainActor in await self.translate(rect, reuseSession: session) }
            }
            lastCaptureSession = session
            if LensSettings.defaultInPlaceOverlay {
                showOverlayDefault(session, near: rect)
            } else {
                show(session, near: rect)
            }
            await session.run()
        } catch LensError.permission {
            permissionAlert()
        } catch {
            let alert = NSAlert()
            alert.messageText = "截取屏幕失败"
            alert.informativeText = error.localizedDescription
            alert.runModal()
        }
    }

    @MainActor
    private func showOverlayDefault(_ session: LensSession, near rect: CGRect) {
        overlayController.show(for: session) { [weak self, weak session] in
            guard let self, let session else { return }
            self.show(session, near: rect)
        }
    }

    @MainActor
    private func show(_ session: LensSession, near rect: CGRect?) {
        let mouse = NSEvent.mouseLocation
        let anchor = rect ?? CGRect(x: mouse.x - 120, y: mouse.y - 80, width: 240, height: 160)
        let screen = NSScreen.screens.first(where: { $0.frame.contains(CGPoint(x: anchor.midX, y: anchor.midY)) }) ?? NSScreen.main!
        let visible = screen.visibleFrame
        let width = min(max(anchor.width * 1.8 + 60, 620), visible.width - 40)
        let height = min(max(anchor.height + 140, 380), visible.height - 40)
        var origin = CGPoint(x: anchor.maxX + 16, y: anchor.maxY - height)
        if origin.x + width > visible.maxX { origin.x = max(visible.minX + 20, anchor.minX - width - 16) }
        if origin.x < visible.minX { origin.x = visible.midX - width / 2 }
        origin.y = min(max(origin.y, visible.minY + 20), visible.maxY - height - 20)
        let window = NSWindow(
            contentRect: NSRect(origin: origin, size: CGSize(width: width, height: height)),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "鹊桥屏译"
        window.isReleasedWhenClosed = false
        window.level = LensSettings.alwaysOnTop ? .floating : .normal
        window.setFrameAutosaveName("QueqiaoLensResultWindow")
        session.onToggleOverlay = { [weak self, weak window, weak session] in
            guard let self, let session else { return }
            window?.orderOut(nil)
            self.overlayController.show(for: session) {
                window?.makeKeyAndOrderFront(nil)
                NSApp.activate(ignoringOtherApps: true)
            }
        }
        let rootView = ResultView(session: session) { [weak window] onTop in
            window?.level = onTop ? .floating : .normal
        }
        window.contentView = NSHostingView(rootView: rootView)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        resultWindows.removeAll { !$0.isVisible }
        resultWindows.append(window)
    }

    private func permissionAlert() {
        let alert = NSAlert()
        alert.messageText = "需要「屏幕录制」权限"
        alert.informativeText = "在「系统设置 → 隐私与安全性 → 录屏与系统录音」里打开「鹊桥屏译」，然后重新打开它。"
        alert.addButton(withTitle: "打开系统设置")
        alert.addButton(withTitle: "取消")
        if alert.runModal() == .alertFirstButtonReturn,
           let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc func openSettings() {
        if settingsWindow == nil {
            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 500, height: 420),
                styleMask: [.titled, .closable],
                backing: .buffered,
                defer: false
            )
            window.title = "鹊桥屏译 · 设置"
            window.isReleasedWhenClosed = false
            window.contentView = NSHostingView(rootView: SettingsView { [weak self] in
                self?.registerHotKeys()
                self?.rebuildStatusMenu()
            })
            window.center()
            settingsWindow = window
        }
        settingsWindow?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func quit() { NSApp.terminate(nil) }
}

struct SettingsView: View {
    @AppStorage("baseURL") private var baseURL = LensSettings.defaultBaseURL
    @AppStorage("apiKey") private var apiKey = LensSettings.defaultAPIKey
    @AppStorage("model") private var model = LensSettings.defaultModel
    @AppStorage("useGlossary") private var useGlossary = true
    @AppStorage("mergeDiagramLabels") private var mergeDiagramLabels = true
    @AppStorage("skipShortLabels") private var skipShortLabels = false
    @AppStorage("preferAccessibility") private var preferAccessibility = true
    @AppStorage("alwaysOnTop") private var alwaysOnTop = true
    @AppStorage("defaultInPlaceOverlay") private var defaultInPlaceOverlay = true
    @AppStorage("showOverlayToolbar") private var showOverlayToolbar = false
    @AppStorage("hotKeyModifiers") private var hotKeyModifiers = "cmd"
    @AppStorage("captureKey") private var captureKey = "O"
    @AppStorage("selectionKey") private var selectionKey = "T"
    @AppStorage("recaptureKey") private var recaptureKey = "R"
    @AppStorage("ocrKey") private var ocrKey = "O"
    @AppStorage("ocrAutoCopy") private var ocrAutoCopy = true
    @State private var launchAtLogin = SMAppService.mainApp.status == .enabled
    var onShortcutsChanged: (() -> Void)? = nil

    var body: some View {
        Form {
            TextField("接口地址", text: $baseURL)
            SecureField("API Key", text: $apiKey)
            HStack {
                TextField("模型", text: $model)
                Menu("常用") {
                    ForEach(LensSettings.suggestedModels, id: \.self) { name in
                        Button(name) { model = name }
                    }
                }
                .fixedSize()
            }
            Divider()
            Toggle("框选后默认使用「原位悬浮模式」打开", isOn: $defaultInPlaceOverlay)
            Toggle("原位悬浮时显示顶部工具栏", isOn: $showOverlayToolbar)
            Toggle("共用鹊桥 AI 术语表（\(Glossary.entries.count) 条）", isOn: $useGlossary)
            Toggle("原生应用优先用辅助功能读取原文（减少 OCR 错字）", isOn: $preferAccessibility)
            Toggle("合并图表内的零散短标签", isOn: $mergeDiagramLabels)
            Toggle("直接跳过图表内的短标签", isOn: $skipShortLabels)
            Toggle("结果窗口默认置顶", isOn: $alwaysOnTop)
            Toggle("OCR 识别后自动复制到剪贴板", isOn: $ocrAutoCopy)
            Toggle("登录 Mac 时自动启动鹊桥屏译", isOn: $launchAtLogin)
                .onChange(of: launchAtLogin) { _, enabled in
                    if enabled {
                        try? SMAppService.mainApp.register()
                    } else {
                        try? SMAppService.mainApp.unregister()
                    }
                }
            Divider()
            HStack(spacing: 12) {
                Picker("修饰键", selection: $hotKeyModifiers) {
                    Text("⌘").tag("cmd")
                    Text("⌥⌘").tag("opt_cmd")
                    Text("⌃⌘").tag("ctrl_cmd")
                    Text("⌥⇧").tag("opt_shift")
                }
                .frame(width: 120)
                .onChange(of: hotKeyModifiers) { _, _ in onShortcutsChanged?() }
                TextField("框选", text: $captureKey).frame(width: 44)
                TextField("划词", text: $selectionKey).frame(width: 44)
                TextField("重截", text: $recaptureKey).frame(width: 44)
                TextField("OCR", text: $ocrKey).frame(width: 44)
                Button("应用") { onShortcutsChanged?() }
            }
            HStack(spacing: 10) {
                Button("检查录屏权限") { _ = CGRequestScreenCaptureAccess() }
                Button("检查辅助功能权限") { _ = AccessibilityReader.isTrusted(prompt: true) }
            }
            Text("当前快捷键：\(LensSettings.modifierSymbol)\(captureKey) 框选翻译 · \(LensSettings.modifierSymbol)\(selectionKey) 划词翻译 · \(LensSettings.modifierSymbol)\(recaptureKey) 重新截取 · \(LensSettings.ocrModifierSymbol)\(ocrKey) 识别文字（OCR）。")
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
        }
        .padding(20)
        .frame(width: 500)
    }
}

/// `--render <image> <out.png> [bilingual|margin] [width height]`: run OCR + translation on an image
/// and render the result window to a PNG, for testing without screen access.
enum Offline {
    /// `--ocr <image>`: print the OCR text (merged paragraphs, then line-preserving) for testing.
    static func ocr(_ path: String) {
        guard let source = NSImage(contentsOfFile: path),
              let image = source.cgImage(forProposedRect: nil, context: nil, hints: nil),
              let raw = try? OCR.recognize(image)
        else {
            FileHandle.standardError.write(Data("cannot read \(path)\n".utf8))
            exit(2)
        }
        let text = TextLayout.recognizedText(from: raw, imageWidth: CGFloat(image.width))
        print("=== \(text.count) paragraphs, merged ===")
        print(text.merged)
        print("=== keep lines ===")
        print(text.lines)
        exit(0)
    }

    static func run(_ args: [String]) {
        guard args.count >= 2,
              let source = NSImage(contentsOfFile: args[0]),
              let image = source.cgImage(forProposedRect: nil, context: nil, hints: nil)
        else {
            FileHandle.standardError.write(Data("usage: --render <image> <out.png> [bilingual|margin] [width height]\n".utf8))
            exit(2)
        }
        let mode: ViewMode = args.count > 2 && args[2] == "margin" ? .margin : .bilingual
        let width = args.count > 3 ? CGFloat(Double(args[3]) ?? 1100) : 1100
        let height = args.count > 4 ? CGFloat(Double(args[4]) ?? 900) : 900
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        Task { @MainActor in
            let session = LensSession(image: image, scale: 2)
            await session.run()
            for paragraph in session.paragraphs {
                let mark = paragraph.skip ? "skip(\(paragraph.skipReason ?? ""))" : (paragraph.cached ? "cache" : (paragraph.isDiagramGroup ? "diag" : "tran"))
                print("[\(paragraph.id)] \(mark) lines=\(paragraph.lineCount) | \(paragraph.text.prefix(70)) || \((paragraph.translation ?? "").prefix(50))")
            }
            print(session.summary)
            let view = ResultView(session: session, mode: mode).frame(width: width, height: height)
            let host = NSHostingView(rootView: view)
            host.frame = NSRect(x: 0, y: 0, width: width, height: height)
            let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
            window.contentView = host
            host.layoutSubtreeIfNeeded()
            try? await Task.sleep(nanoseconds: 300_000_000)
            host.layoutSubtreeIfNeeded()
            if let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) {
                host.cacheDisplay(in: host.bounds, to: rep)
                try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: args[1]))
            }
            exit(0)
        }
        app.run()
    }
}
