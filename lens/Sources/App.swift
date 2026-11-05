import AppKit
import Carbon.HIToolbox
import SwiftUI

@main
struct LensMain {
    static func main() {
        if let index = CommandLine.arguments.firstIndex(of: "--render") {
            Offline.run(Array(CommandLine.arguments[(index + 1)...]))
            return
        }
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private let selection = SelectionController()
    private var hotKey: EventHotKeyRef?
    private var resultWindows: [NSWindow] = []
    private var settingsWindow: NSWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "character.book.closed", accessibilityDescription: "鹊桥屏译")
            button.image?.isTemplate = true
        }
        let menu = NSMenu()
        menu.addItem(withTitle: "框选翻译（⌥⇧T）", action: #selector(startSelection), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "设置…", action: #selector(openSettings), keyEquivalent: ",").target = self
        menu.addItem(withTitle: "退出鹊桥屏译", action: #selector(quit), keyEquivalent: "q").target = self
        statusItem.menu = menu
        registerHotKey()
        if !CGPreflightScreenCaptureAccess() { CGRequestScreenCaptureAccess() }
    }

    /// ⌥⇧T anywhere starts a selection.
    private func registerHotKey() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        let pointer = Unmanaged.passUnretained(self).toOpaque()
        InstallEventHandler(GetApplicationEventTarget(), { _, _, user in
            guard let user else { return noErr }
            let delegate = Unmanaged<AppDelegate>.fromOpaque(user).takeUnretainedValue()
            DispatchQueue.main.async { delegate.startSelection() }
            return noErr
        }, 1, &spec, pointer, nil)
        let id = EventHotKeyID(signature: OSType(0x5151_4C53), id: 1)
        RegisterEventHotKey(UInt32(kVK_ANSI_T), UInt32(optionKey | shiftKey), id, GetApplicationEventTarget(), 0, &hotKey)
    }

    @objc func startSelection() {
        selection.begin { [weak self] rect in
            guard let self, let rect else { return }
            Task { @MainActor in await self.translate(rect) }
        }
    }

    @MainActor
    private func translate(_ rect: CGRect) async {
        do {
            let (image, scale) = try await ScreenCapture.capture(rect)
            let session = LensSession(image: image, scale: scale)
            show(session, near: rect)
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
    private func show(_ session: LensSession, near rect: CGRect) {
        let screen = NSScreen.screens.first(where: { $0.frame.contains(CGPoint(x: rect.midX, y: rect.midY)) }) ?? NSScreen.main!
        let visible = screen.visibleFrame
        let width = min(max(rect.width * 1.9 + 60, 640), visible.width - 40)
        let height = min(max(rect.height + 140, 420), visible.height - 40)
        var origin = CGPoint(x: rect.maxX + 16, y: rect.maxY - height)
        if origin.x + width > visible.maxX { origin.x = max(visible.minX + 20, rect.minX - width - 16) }
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
        window.level = .floating
        window.contentView = NSHostingView(rootView: ResultView(session: session))
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
                contentRect: NSRect(x: 0, y: 0, width: 460, height: 230),
                styleMask: [.titled, .closable],
                backing: .buffered,
                defer: false
            )
            window.title = "鹊桥屏译 · 设置"
            window.isReleasedWhenClosed = false
            window.contentView = NSHostingView(rootView: SettingsView())
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

    var body: some View {
        Form {
            TextField("接口地址", text: $baseURL)
            SecureField("API Key", text: $apiKey)
            TextField("模型", text: $model)
            Text("任何 OpenAI 兼容的 /v1/chat/completions 接口都可以；默认是本机的 Magpie。快捷键 ⌥⇧T 框选翻译。")
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
        }
        .padding(20)
        .frame(width: 460)
    }
}

/// `--render <image> <out.png> [bilingual|margin] [width height]`: run OCR + translation on an image
/// and render the result window to a PNG, for testing without screen access.
enum Offline {
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
                let mark = paragraph.skip ? "skip" : "tran"
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
