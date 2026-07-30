// HDDCATShell.swift - the native macOS window that HDDCAT.app actually opens.
//
// Replaces the old bash launcher, which ran `catalog.py serve` and let Python
// pop the user's default browser open on 127.0.0.1. Same web UI, same server -
// it just renders inside our own window now, with our own Dock icon, instead of
// landing as one more tab in Chrome.
//
// What it does, in order:
//   1. spawns `python3 -u catalog.py serve --no-browser` with cwd = ~/HDDCAT
//   2. reads the server's stdout until it prints the URL it actually bound to
//      (catalog.py walks the port range when 8765 is busy, so we never guess)
//   3. loads that URL in a WKWebView filling the window
//   4. kills the server when the app quits
//
// Links that point off 127.0.0.1 (Touchnewmedia, GitHub, Buy Me a Coffee) open
// in the real browser - an app window is a bad place to browse the web.
//
// Dev overrides, all optional: HDDCAT_HOME (working dir), HDDCAT_DB (--db),
// HDDCAT_PORT (starting port), HDDCAT_CATALOG (path to catalog.py when running
// the binary outside an .app bundle), HDDCAT_DEBUG=1 (Web Inspector).
//
// Build: shell/build-shell.sh  (universal + ad-hoc signature)

import AppKit
import WebKit

// MARK: - inline pages (no assets, no network - shown before/instead of the UI)

private func loadingPage() -> String {
    return """
    <!DOCTYPE html><html lang="th"><head><meta charset="utf-8">
    <style>
      html,body{height:100%;margin:0}
      body{display:flex;align-items:center;justify-content:center;
           background:#FBFCFD;color:#05011C;
           font-family:-apple-system,"Helvetica Neue",sans-serif}
      .box{text-align:center}
      .dot{width:14px;height:14px;border-radius:50%;background:#6633EE;
           margin:0 auto 18px;animation:p 1.1s ease-in-out infinite}
      @keyframes p{0%,100%{opacity:.25;transform:scale(.8)}50%{opacity:1;transform:scale(1)}}
      p{margin:0;font-size:15px;color:#404040}
    </style></head><body><div class="box">
      <div class="dot"></div><p>กำลังเปิด HDDCAT…</p>
    </div></body></html>
    """
}

private func errorPage(title: String, detail: String) -> String {
    let safe = detail
        .replacingOccurrences(of: "&", with: "&amp;")
        .replacingOccurrences(of: "<", with: "&lt;")
        .replacingOccurrences(of: ">", with: "&gt;")
    return """
    <!DOCTYPE html><html lang="th"><head><meta charset="utf-8">
    <style>
      html,body{height:100%;margin:0}
      body{display:flex;align-items:center;justify-content:center;
           background:#FBFCFD;color:#05011C;
           font-family:-apple-system,"Helvetica Neue",sans-serif;padding:40px}
      .box{max-width:620px}
      h1{font-size:19px;margin:0 0 10px}
      p{font-size:14px;color:#404040;line-height:1.65;margin:0 0 14px}
      pre{background:#fff;border:1px solid #E7E7EF;border-radius:10px;padding:14px;
          font-size:12px;color:#404040;white-space:pre-wrap;word-break:break-word;
          max-height:260px;overflow:auto;margin:0}
    </style></head><body><div class="box">
      <h1>\(title)</h1>
      <p>HDDCAT ต้องใช้ python3 ที่มากับ macOS (Command Line Tools) ถ้ายังไม่ได้ติดตั้ง
         ให้เปิด Terminal แล้วรัน <code>xcode-select --install</code> หนึ่งครั้ง
         แล้วเปิดแอปใหม่</p>
      <pre>\(safe.isEmpty ? "(ไม่มีรายละเอียดเพิ่มเติม)" : safe)</pre>
    </div></body></html>
    """
}

// MARK: - app delegate

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {

    private var window: NSWindow!
    private var webView: WKWebView!
    private var server: Process?
    private var serverURL: URL?
    private var quitting = false
    private var launchFailed = false
    private var stderrTail = ""
    private let stderrLock = NSLock()
    private var zoom = 1.0
    private var signalSources: [DispatchSourceSignal] = []

    // MARK: window

    func applicationDidFinishLaunching(_ note: Notification) {
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()   // persistent, so the UI keeps its own prefs
        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1280, height: 840),
                            configuration: cfg)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        if #available(macOS 13.3, *) {
            webView.isInspectable = ProcessInfo.processInfo.environment["HDDCAT_DEBUG"] != nil
        }

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 840),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "HDDCAT"
        // no visible title text - the web UI has its own floating navbar, and a
        // second header stacked on top of it looks like a browser
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.backgroundColor = .white
        window.minSize = NSSize(width: 960, height: 600)
        window.contentView = webView
        window.center()
        window.setFrameAutosaveName("HDDCATMainWindow")   // remembers size/position
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        installSignalHandlers()
        webView.loadHTMLString(loadingPage(), baseURL: nil)
        startServer()
    }

    /// A plain SIGTERM (pkill, logout, Force Quit) kills a Cocoa app without
    /// ever calling applicationWillTerminate, which would strand the server.
    private func installSignalHandlers() {
        for sig in [SIGTERM, SIGINT] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { [weak self] in
                self?.stopServer()
                exit(0)
            }
            source.resume()
            signalSources.append(source)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool {
        return true
    }

    func applicationWillTerminate(_ note: Notification) {
        stopServer()
    }

    // MARK: server process

    /// catalog.py inside the bundle; falls back to HDDCAT_CATALOG for dev runs of
    /// the bare binary.
    private func catalogPath() -> String? {
        if let res = Bundle.main.resourceURL?.appendingPathComponent("catalog.py").path,
           FileManager.default.isReadableFile(atPath: res) {
            return res
        }
        if let dev = ProcessInfo.processInfo.environment["HDDCAT_CATALOG"],
           FileManager.default.isReadableFile(atPath: dev) {
            return dev
        }
        return nil
    }

    /// Apple's own python3, but only once Command Line Tools are actually
    /// installed - otherwise /usr/bin/python3 is a stub that pops the "install
    /// developer tools" dialog. Preferred over PATH because python.org builds
    /// ship without CA certificates, which silently breaks the update check.
    private func applePython() -> String? {
        guard FileManager.default.isExecutableFile(atPath: "/usr/bin/python3") else { return nil }
        let probe = Process()
        probe.executableURL = URL(fileURLWithPath: "/usr/bin/xcode-select")
        probe.arguments = ["-p"]
        probe.standardOutput = FileHandle.nullDevice
        probe.standardError = FileHandle.nullDevice
        do {
            try probe.run()
            probe.waitUntilExit()
            return probe.terminationStatus == 0 ? "/usr/bin/python3" : nil
        } catch {
            return nil
        }
    }

    private func startServer() {
        let env = ProcessInfo.processInfo.environment
        // user data lives in ~/HDDCAT, never inside the bundle - /Applications is
        // typically not user-writable and the bundle gets replaced on update
        let home = env["HDDCAT_HOME"]
            ?? (NSHomeDirectory() as NSString).appendingPathComponent("HDDCAT")
        try? FileManager.default.createDirectory(atPath: home,
                                                 withIntermediateDirectories: true)

        guard let catalog = catalogPath() else {
            fail(title: "ไม่พบไฟล์ catalog.py ในแอป",
                 detail: "HDDCAT.app เสียหายหรือถูกแตกไฟล์ไม่ครบ - ลองโหลด HDDCAT.zip ใหม่")
            return
        }

        let p = Process()
        // -u: without it Python block-buffers stdout into the pipe and we'd never
        // see the "web UI: http://..." line until the buffer filled
        var args = ["-u", catalog]
        if let db = env["HDDCAT_DB"], !db.isEmpty { args += ["--db", db] }
        // --exit-with-parent: if this app is force-quit or crashes we never get
        // to terminate the child, and the orphan keeps the port and the DB
        args += ["serve", "--no-browser", "--exit-with-parent",
                 "--port", env["HDDCAT_PORT"] ?? "8765"]

        if let apple = applePython() {
            p.executableURL = URL(fileURLWithPath: apple)
            p.arguments = args
        } else {
            // no Command Line Tools - fall back to whatever python3 is on PATH
            p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            p.arguments = ["python3"] + args
        }
        p.currentDirectoryURL = URL(fileURLWithPath: home)

        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err

        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            self?.scanForURL(in: text)
        }
        err.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            self?.appendStderr(text)
        }

        p.terminationHandler = { [weak self] proc in
            guard let self = self, !self.quitting else { return }
            DispatchQueue.main.async {
                if self.serverURL == nil {
                    self.fail(title: "เปิดเซิร์ฟเวอร์ในเครื่องไม่สำเร็จ",
                              detail: self.currentStderr())
                } else {
                    self.fail(title: "เซิร์ฟเวอร์หยุดทำงาน (exit \(proc.terminationStatus))",
                              detail: self.currentStderr())
                }
            }
        }

        do {
            try p.run()
            server = p
        } catch {
            fail(title: "เรียก python3 ไม่สำเร็จ", detail: "\(error)")
            return
        }

        // don't hang on the loading dot forever if the server never announces itself
        DispatchQueue.main.asyncAfter(deadline: .now() + 25) { [weak self] in
            guard let self = self, self.serverURL == nil, !self.launchFailed else { return }
            self.fail(title: "เซิร์ฟเวอร์ไม่ตอบสนองภายใน 25 วินาที",
                      detail: self.currentStderr())
        }
    }

    /// catalog.py prints `HDD Catalog web UI: http://127.0.0.1:<port>/` once it has
    /// bound a port - that line is the handshake.
    private func scanForURL(in text: String) {
        guard serverURL == nil, let range = text.range(of: "http://127.0.0.1:") else { return }
        let rest = text[range.lowerBound...]
        let raw = rest.prefix(while: { !$0.isWhitespace })
        guard let url = URL(string: String(raw)) else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self = self, self.serverURL == nil else { return }
            self.serverURL = url
            self.webView.load(URLRequest(url: url))
        }
    }

    private func appendStderr(_ text: String) {
        stderrLock.lock()
        stderrTail += text
        if stderrTail.count > 4000 { stderrTail = String(stderrTail.suffix(4000)) }
        stderrLock.unlock()
    }

    private func currentStderr() -> String {
        stderrLock.lock()
        defer { stderrLock.unlock() }
        return stderrTail.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func fail(title: String, detail: String) {
        launchFailed = true
        webView.loadHTMLString(errorPage(title: title, detail: detail), baseURL: nil)
    }

    private func stopServer() {
        guard let p = server, p.isRunning else { return }
        quitting = true
        p.terminate()
        let deadline = Date().addingTimeInterval(3)
        while p.isRunning && Date() < deadline { usleep(50_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }

    // MARK: navigation

    func webView(_ webView: WKWebView,
                 decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.allow); return }
        let scheme = url.scheme?.lowercased() ?? ""
        if scheme == "about" || scheme == "data" || scheme == "blob" {
            decisionHandler(.allow); return
        }
        let host = url.host ?? ""
        if (scheme == "http" || scheme == "https") && (host == "127.0.0.1" || host == "localhost") {
            decisionHandler(.allow); return
        }
        NSWorkspace.shared.open(url)   // the real web belongs in the real browser
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard let url = webView.url, url.isFileURL == false, url.scheme != "about" else { return }
        debugLog("loaded \(url.absoluteString)")
    }

    /// Without these the window would sit on the loading dot forever if the page
    /// itself failed, even though the server came up fine.
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        guard serverURL != nil else { return }
        fail(title: "โหลดหน้าไม่สำเร็จ", detail: error.localizedDescription)
    }

    func webView(_ webView: WKWebView,
                 didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        guard serverURL != nil else { return }
        fail(title: "เชื่อมต่อเซิร์ฟเวอร์ในเครื่องไม่ได้", detail: error.localizedDescription)
    }

    private func debugLog(_ msg: String) {
        guard ProcessInfo.processInfo.environment["HDDCAT_DEBUG"] != nil else { return }
        FileHandle.standardError.write("[HDDCAT] \(msg)\n".data(using: .utf8)!)
    }

    /// target="_blank" links (Touchnewmedia, GitHub, BMC) - never open a second window
    func webView(_ webView: WKWebView,
                 createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // MARK: menu actions

    @objc func reloadPage(_ sender: Any?) {
        if let url = serverURL, launchFailed {
            launchFailed = false
            webView.load(URLRequest(url: url))
        } else {
            webView.reload()
        }
    }

    @objc func zoomIn(_ sender: Any?) { setZoom(min(zoom + 0.1, 2.0)) }
    @objc func zoomOut(_ sender: Any?) { setZoom(max(zoom - 0.1, 0.6)) }
    @objc func zoomReset(_ sender: Any?) { setZoom(1.0) }

    /// CSS zoom rather than WKWebView.pageZoom - that API is macOS 14+ and this
    /// app still targets 11.
    private func setZoom(_ value: Double) {
        zoom = value
        webView.evaluateJavaScript("document.documentElement.style.zoom='\(value)'",
                                    completionHandler: nil)
    }
}

// MARK: - menu bar

private func buildMenu(_ delegate: AppDelegate) {
    let main = NSMenu()

    let appItem = NSMenuItem()
    main.addItem(appItem)
    let appMenu = NSMenu()
    appMenu.addItem(withTitle: "เกี่ยวกับ HDDCAT",
                    action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
    appMenu.addItem(.separator())
    appMenu.addItem(withTitle: "ซ่อน HDDCAT",
                    action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
    let hideOthers = appMenu.addItem(withTitle: "ซ่อนแอปอื่น",
                                      action: #selector(NSApplication.hideOtherApplications(_:)),
                                      keyEquivalent: "h")
    hideOthers.keyEquivalentModifierMask = [.command, .option]
    appMenu.addItem(withTitle: "แสดงทั้งหมด",
                    action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
    appMenu.addItem(.separator())
    appMenu.addItem(withTitle: "ออกจาก HDDCAT",
                    action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    appItem.submenu = appMenu

    // without a real Edit menu, Cmd+C/V/A don't work in the web UI's search box
    let editItem = NSMenuItem()
    main.addItem(editItem)
    let editMenu = NSMenu(title: "แก้ไข")
    editMenu.addItem(withTitle: "เลิกทำ", action: Selector(("undo:")), keyEquivalent: "z")
    let redo = editMenu.addItem(withTitle: "ทำซ้ำ", action: Selector(("redo:")), keyEquivalent: "z")
    redo.keyEquivalentModifierMask = [.command, .shift]
    editMenu.addItem(.separator())
    editMenu.addItem(withTitle: "ตัด", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
    editMenu.addItem(withTitle: "คัดลอก", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
    editMenu.addItem(withTitle: "วาง", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
    editMenu.addItem(withTitle: "เลือกทั้งหมด", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
    editItem.submenu = editMenu

    let viewItem = NSMenuItem()
    main.addItem(viewItem)
    let viewMenu = NSMenu(title: "มุมมอง")
    let reload = viewMenu.addItem(withTitle: "โหลดใหม่",
                                   action: #selector(AppDelegate.reloadPage(_:)), keyEquivalent: "r")
    reload.target = delegate
    viewMenu.addItem(.separator())
    let zin = viewMenu.addItem(withTitle: "ขยาย",
                                action: #selector(AppDelegate.zoomIn(_:)), keyEquivalent: "+")
    zin.target = delegate
    let zout = viewMenu.addItem(withTitle: "ย่อ",
                                 action: #selector(AppDelegate.zoomOut(_:)), keyEquivalent: "-")
    zout.target = delegate
    let zreset = viewMenu.addItem(withTitle: "ขนาดจริง",
                                   action: #selector(AppDelegate.zoomReset(_:)), keyEquivalent: "0")
    zreset.target = delegate
    viewItem.submenu = viewMenu

    let windowItem = NSMenuItem()
    main.addItem(windowItem)
    let windowMenu = NSMenu(title: "หน้าต่าง")
    windowMenu.addItem(withTitle: "ย่อเก็บ",
                       action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
    windowMenu.addItem(withTitle: "ซูม", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
    windowItem.submenu = windowMenu

    NSApp.mainMenu = main
    NSApp.windowsMenu = windowMenu
}

// MARK: - entry point

@main
enum HDDCATShell {
    static let delegate = AppDelegate()

    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)   // Dock icon + menu bar, not a background agent
        app.delegate = delegate
        buildMenu(delegate)
        app.run()
    }
}
