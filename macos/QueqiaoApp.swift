// 鹊桥 Queqiao — macOS wrapper.
//
// Starts the bundled Python service (Contents/Resources/runtime + server) on a free local port,
// waits until it answers /api/health, and shows the web UI in a native window. The service is
// stopped when the app quits. Downloads (PDF / source zip) are saved to ~/Downloads.

import AppKit
import WebKit

final class ServiceController {
    private(set) var port: Int = 0
    private var process: Process?
    let logURL: URL
    let dataURL: URL

    init() {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        dataURL = support.appendingPathComponent("Queqiao", isDirectory: true)
        let logs = FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Logs/Queqiao", isDirectory: true)
        try? FileManager.default.createDirectory(at: dataURL, withIntermediateDirectories: true)
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        logURL = logs.appendingPathComponent("server.log")
    }

    static func freePort() -> Int {
        let socketFD = socket(AF_INET, SOCK_STREAM, 0)
        guard socketFD >= 0 else { return 18686 }
        defer { close(socketFD) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = 0
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        var bound = address
        let result = withUnsafePointer(to: &bound) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(socketFD, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        guard result == 0 else { return 18686 }
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &bound) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(socketFD, $0, &length) }
        }
        return Int(UInt16(bigEndian: bound.sin_port))
    }

    func start() throws {
        guard let resources = Bundle.main.resourceURL else { throw NSError(domain: "Queqiao", code: 1) }
        let python = resources.appendingPathComponent("runtime/bin/python3")
        let server = resources.appendingPathComponent("server")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            throw NSError(domain: "Queqiao", code: 2, userInfo: [NSLocalizedDescriptionKey: "应用内缺少 Python 运行时：\(python.path)"])
        }
        port = ServiceController.freePort()
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        let log = try FileHandle(forWritingTo: logURL)

        let task = Process()
        task.executableURL = python
        task.arguments = ["-m", "uvicorn", "app.main:asgi", "--host", "127.0.0.1", "--port", String(port), "--log-level", "warning"]
        task.currentDirectoryURL = server
        var environment = ProcessInfo.processInfo.environment
        // Finder-launched apps get a minimal PATH; add the usual TeX and Homebrew locations.
        let extraPaths = ["/Library/TeX/texbin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        environment["PATH"] = (extraPaths + [environment["PATH"] ?? ""]).joined(separator: ":")
        environment["QUEQIAO_DATA_DIR"] = dataURL.path
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        environment.removeValue(forKey: "PYTHONPATH")
        environment.removeValue(forKey: "PYTHONHOME")
        task.environment = environment
        task.standardOutput = log
        task.standardError = log
        try task.run()
        process = task
    }

    func waitUntilReady(timeout: TimeInterval, completion: @escaping (Bool) -> Void) {
        let deadline = Date().addingTimeInterval(timeout)
        let url = URL(string: "http://127.0.0.1:\(port)/api/health")!
        func attempt() {
            if let process, !process.isRunning {
                DispatchQueue.main.async { completion(false) }
                return
            }
            var request = URLRequest(url: url)
            request.timeoutInterval = 2
            URLSession.shared.dataTask(with: request) { _, response, _ in
                if (response as? HTTPURLResponse)?.statusCode == 200 {
                    DispatchQueue.main.async { completion(true) }
                } else if Date() > deadline {
                    DispatchQueue.main.async { completion(false) }
                } else {
                    DispatchQueue.global().asyncAfter(deadline: .now() + 0.4) { attempt() }
                }
            }.resume()
        }
        attempt()
    }

    func stop() {
        guard let process, process.isRunning else { return }
        process.terminate()
        let deadline = Date().addingTimeInterval(3)
        while process.isRunning && Date() < deadline { usleep(50_000) }
        if process.isRunning { kill(process.processIdentifier, SIGKILL) }
    }

    func tailOfLog(_ limit: Int = 2400) -> String {
        guard let data = try? Data(contentsOf: logURL) else { return "" }
        return String(decoding: data.suffix(limit), as: UTF8.self)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private let service = ServiceController()
    private var downloadDestinations: [ObjectIdentifier: URL] = [:]

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        showSplash("正在启动鹊桥…")
        do {
            try service.start()
        } catch {
            fail("无法启动服务：\(error.localizedDescription)")
            return
        }
        service.waitUntilReady(timeout: 90) { [weak self] ready in
            guard let self else { return }
            if ready {
                self.webView.load(URLRequest(url: URL(string: "http://127.0.0.1:\(self.service.port)/")!))
            } else {
                self.fail("服务没有在 90 秒内启动。\n\n日志：\(self.service.logURL.path)\n\n\(self.service.tailOfLog())")
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        service.stop()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    // MARK: UI

    private func buildWindow() {
        let configuration = WKWebViewConfiguration()
        configuration.preferences.setValue(true, forKey: "developerExtrasEnabled")
        configuration.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.setValue(false, forKey: "drawsBackground")

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1360, height: 880),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "鹊桥 Queqiao"
        window.minSize = NSSize(width: 820, height: 560)
        window.contentView = webView
        window.setFrameAutosaveName("QueqiaoMainWindow")
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func showSplash(_ message: String) {
        let html = """
        <html><body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
        background:#f6f3ee;color:#4f4b45;font:15px -apple-system,'PingFang SC',sans-serif">
        <div style="text-align:center"><div style="font:700 30px Georgia,'Songti SC',serif;color:#1d1b18">鹊桥 <i style="color:#8b2e3c;font-weight:400">Queqiao</i></div>
        <p style="margin-top:14px">\(message)</p></div></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    private func fail(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "鹊桥启动失败"
        alert.informativeText = message
        alert.addButton(withTitle: "打开日志")
        alert.addButton(withTitle: "退出")
        if alert.runModal() == .alertFirstButtonReturn {
            NSWorkspace.shared.open(service.logURL)
        }
        NSApp.terminate(nil)
    }

    private func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于鹊桥", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "在浏览器中打开", action: #selector(openInBrowser), keyEquivalent: "b")
        appMenu.addItem(withTitle: "打开数据文件夹", action: #selector(openDataFolder), keyEquivalent: "")
        appMenu.addItem(withTitle: "查看日志", action: #selector(openLog), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏鹊桥", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "退出鹊桥", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let viewMenu = NSMenu(title: "显示")
        viewMenu.addItem(withTitle: "首页", action: #selector(goHome), keyEquivalent: "1")
        viewMenu.addItem(withTitle: "文库", action: #selector(goLibrary), keyEquivalent: "2")
        viewMenu.addItem(withTitle: "设置", action: #selector(goSettings), keyEquivalent: ",")
        viewMenu.addItem(.separator())
        viewMenu.addItem(withTitle: "后退", action: #selector(goBack), keyEquivalent: "[")
        viewMenu.addItem(withTitle: "重新载入", action: #selector(reload), keyEquivalent: "r")
        viewMenu.addItem(withTitle: "进入全屏", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        viewItem.submenu = viewMenu
        main.addItem(viewItem)

        let windowItem = NSMenuItem()
        let windowMenu = NSMenu(title: "窗口")
        windowMenu.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "关闭", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        windowItem.submenu = windowMenu
        main.addItem(windowItem)
        NSApp.mainMenu = main
        NSApp.windowsMenu = windowMenu
    }

    private func open(path: String) {
        guard service.port > 0, let url = URL(string: "http://127.0.0.1:\(service.port)\(path)") else { return }
        webView.load(URLRequest(url: url))
    }

    @objc private func goHome() { open(path: "/") }
    @objc private func goLibrary() { open(path: "/library") }
    @objc private func goSettings() { open(path: "/settings") }
    @objc private func goBack() { webView.goBack() }
    @objc private func reload() { webView.reload() }

    @objc private func openInBrowser() {
        let target = webView.url?.host == "127.0.0.1" ? webView.url : URL(string: "http://127.0.0.1:\(service.port)/")
        if let target { NSWorkspace.shared.open(target) }
    }

    @objc private func openDataFolder() { NSWorkspace.shared.open(service.dataURL) }
    @objc private func openLog() { NSWorkspace.shared.open(service.logURL) }

    // MARK: Navigation, popups and downloads

    private func isLocal(_ url: URL?) -> Bool {
        guard let url else { return true }
        if url.scheme == "about" || url.scheme == "blob" || url.scheme == "data" { return true }
        return url.host == "127.0.0.1" && url.port == service.port
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.shouldPerformDownload {
            decisionHandler(.download)
            return
        }
        if let url = navigationAction.request.url, navigationAction.targetFrame?.isMainFrame != false, !isLocal(url) {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if let response = navigationResponse.response as? HTTPURLResponse,
           let disposition = response.value(forHTTPHeaderField: "Content-Disposition"),
           disposition.lowercased().hasPrefix("attachment") {
            decisionHandler(.download)
            return
        }
        decisionHandler(navigationResponse.canShowMIMEType ? .allow : .download)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url {
            if isLocal(url) { webView.load(navigationAction.request) } else { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let folder = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        var target = folder.appendingPathComponent(suggestedFilename)
        let stem = target.deletingPathExtension().lastPathComponent
        let ext = target.pathExtension
        var counter = 1
        while FileManager.default.fileExists(atPath: target.path) {
            counter += 1
            target = folder.appendingPathComponent(ext.isEmpty ? "\(stem) \(counter)" : "\(stem) \(counter).\(ext)")
        }
        downloadDestinations[ObjectIdentifier(download)] = target
        completionHandler(target)
    }

    func downloadDidFinish(_ download: WKDownload) {
        if let target = downloadDestinations.removeValue(forKey: ObjectIdentifier(download)) {
            NSWorkspace.shared.activateFileViewerSelecting([target])
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        downloadDestinations.removeValue(forKey: ObjectIdentifier(download))
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "好")
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
