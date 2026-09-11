// Яндекс.Диск: приложение в строке меню.
//
// Сборка:  swiftc -O YandexSync.swift -o ~/bin/YandexSync
//
// Вся работа — в yasync.py; здесь только выбор папок и решение, КОГДА запускать.
//
// Про батарею. Ни одного цикла опроса: приложение спит, пока ядро его не
// разбудит. Источники пробуждения ровно четыре:
//   1. FSEvents на корне — файл изменился локально (push из ядра, не опрос);
//   2. Finder стал активным — считаем, что папку могли открыть, тянем свежее;
//   3. пробуждение из сна;
//   4. сеть вернулась после обрыва.
// Отдельного «событие открытия папки» в macOS нет — такого API не существует,
// поэтому пункт 2 это приближение, и он придушен тайм-аутом.
import Cocoa
import CoreServices
import Network

let HOME = NSHomeDirectory()
let YASYNC = HOME + "/bin/yasync.py"
let AGENT = HOME + "/Library/LaunchAgents/com.local.yandexsync.plist"

// Finder открывают часто, а синхронизация — сетевой запрос. Не чаще раза в 2 минуты.
let FINDER_THROTTLE: TimeInterval = 120
// Пачку сохранений подряд сливаем одним запуском.
let EDIT_DEBOUNCE: TimeInterval = 10
// Наша же запись файлов поднимает FSEvents. Без этого окна получается вечный круг.
let SELF_ECHO: TimeInterval = 5

// ----------------------------------------------------------------- движок
@discardableResult
func yasync(_ args: [String]) -> (code: Int32, out: String) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
    p.arguments = [YASYNC] + args
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = Pipe()
    do { try p.run() } catch { return (-1, "") }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    return (p.terminationStatus, String(data: data, encoding: .utf8) ?? "")
}

struct FolderState: Codable {
    let remote: String
    let local: String
    let lastSync: String?
    let lastResult: String?
    let lastReason: String?
    let files: Int
    let bytes: Int
    let conflicts: Int
}
struct ConflictRef: Codable { let folder: String; let file: String }
struct SyncState: Codable {
    let root: String
    let log: String
    let folders: [FolderState]
    let conflicts: [ConflictRef]
}
struct RemoteItem: Codable {
    let name: String
    let path: String
    let taken: Bool
    let inside: String?
}
struct RemoteListing: Codable {
    let path: String
    let items: [RemoteItem]?
    let error: String?
}

func loadState() -> SyncState? {
    let r = yasync(["state"])
    guard r.code == 0, let d = r.out.data(using: .utf8) else { return nil }
    return try? JSONDecoder().decode(SyncState.self, from: d)
}

// ------------------------------------------------------------- наблюдатель
final class Watcher {
    private var stream: FSEventStreamRef?
    private let onChange: ([String]) -> Void

    init(path: String, onChange: @escaping ([String]) -> Void) {
        self.onChange = onChange
        var ctx = FSEventStreamContext(version: 0,
                                       info: Unmanaged.passUnretained(self).toOpaque(),
                                       retain: nil, release: nil, copyDescription: nil)
        let cb: FSEventStreamCallback = { _, info, count, paths, _, _ in
            guard let info = info else { return }
            let me = Unmanaged<Watcher>.fromOpaque(info).takeUnretainedValue()
            let list = unsafeBitCast(paths, to: NSArray.self) as? [String] ?? []
            if count > 0 { me.onChange(list) }
        }
        // Задержка 2 с — коалесценция уже в ядре: пачка сохранений придёт одним
        // событием, и приложение не будит процессор на каждый байт.
        stream = FSEventStreamCreate(kCFAllocatorDefault, cb, &ctx,
                                     [path] as CFArray,
                                     FSEventStreamEventId(kFSEventStreamEventIdSinceNow),
                                     2.0,
                                     UInt32(kFSEventStreamCreateFlagFileEvents |
                                            kFSEventStreamCreateFlagNoDefer))
        if let s = stream {
            FSEventStreamSetDispatchQueue(s, DispatchQueue.main)
            FSEventStreamStart(s)
        }
    }

    deinit {
        if let s = stream {
            FSEventStreamStop(s)
            FSEventStreamInvalidate(s)
            FSEventStreamRelease(s)
        }
    }
}

// ------------------------------------------------------------ выбор папок
final class PickerWindow: NSWindowController, NSTableViewDataSource, NSTableViewDelegate {
    private let table = NSTableView()
    private let pathLabel = NSTextField(labelWithString: "Яндекс.Диск")
    private let spinner = NSProgressIndicator()
    private var items: [RemoteItem] = []
    private var path = ""
    private let onChanged: () -> Void

    init(onChanged: @escaping () -> Void) {
        self.onChanged = onChanged
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 460, height: 420),
                         styleMask: [.titled, .closable, .resizable],
                         backing: .buffered, defer: false)
        w.title = "Папки Яндекс.Диска"
        w.center()
        super.init(window: w)
        build()
        reload("")
    }
    required init?(coder: NSCoder) { fatalError() }

    private func build() {
        guard let w = window else { return }
        let up = NSButton(title: "Наверх", target: self, action: #selector(goUp))
        up.bezelStyle = .rounded
        pathLabel.lineBreakMode = .byTruncatingHead
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isDisplayedWhenStopped = false

        let bar = NSStackView(views: [up, pathLabel, NSView(), spinner])
        bar.orientation = .horizontal
        bar.spacing = 8
        bar.edgeInsets = NSEdgeInsets(top: 8, left: 12, bottom: 8, right: 12)

        table.addTableColumn({ let c = NSTableColumn(identifier: .init("pick"))
                              c.title = "Синхронизировать"; c.width = 300; return c }())
        table.addTableColumn({ let c = NSTableColumn(identifier: .init("open"))
                              c.title = ""; c.width = 110; return c }())
        table.dataSource = self
        table.delegate = self
        table.rowHeight = 24
        table.usesAlternatingRowBackgroundColors = true
        let scroll = NSScrollView()
        scroll.documentView = table
        scroll.hasVerticalScroller = true

        let hint = NSTextField(wrappingLabelWithString:
            "Галочка — папка синхронизируется целиком в \(HOME)/YandexDisk. " +
            "Первая синхронизация может занять время. Снятие галочки останавливает " +
            "синхронизацию, локальные файлы остаются на месте.")
        hint.font = .systemFont(ofSize: 11)
        hint.textColor = .secondaryLabelColor

        let stack = NSStackView(views: [bar, scroll, hint])
        stack.orientation = .vertical
        stack.spacing = 6
        stack.edgeInsets = NSEdgeInsets(top: 0, left: 12, bottom: 12, right: 12)
        stack.setHuggingPriority(.defaultLow, for: .vertical)
        w.contentView = stack
    }

    private func reload(_ p: String) {
        path = p
        pathLabel.stringValue = p.isEmpty ? "Яндекс.Диск" : "Яндекс.Диск / " + p
        spinner.startAnimation(nil)
        DispatchQueue.global().async {
            let r = yasync(["ls", p, "--json"])
            let listing = (r.out.data(using: .utf8)).flatMap {
                try? JSONDecoder().decode(RemoteListing.self, from: $0)
            }
            DispatchQueue.main.async {
                self.spinner.stopAnimation(nil)
                if let err = listing?.error {
                    self.pathLabel.stringValue = "Не удалось прочитать Диск: " + err
                }
                self.items = listing?.items ?? []
                self.table.reloadData()
            }
        }
    }

    @objc private func goUp() {
        guard !path.isEmpty else { return }
        var parts = path.split(separator: "/").map(String.init)
        parts.removeLast()
        reload(parts.joined(separator: "/"))
    }

    @objc private func descend(_ sender: NSButton) {
        reload(items[sender.tag].path)
    }

    @objc private func toggle(_ sender: NSButton) {
        let it = items[sender.tag]
        let add = sender.state == .on
        sender.isEnabled = false
        spinner.startAnimation(nil)
        DispatchQueue.global().async {
            yasync([add ? "add" : "rm", it.path])
            DispatchQueue.main.async {
                self.spinner.stopAnimation(nil)
                self.onChanged()
                self.reload(self.path)
            }
        }
    }

    func numberOfRows(in tableView: NSTableView) -> Int { items.count }

    func tableView(_ t: NSTableView, viewFor column: NSTableColumn?, row: Int) -> NSView? {
        let it = items[row]
        if column?.identifier.rawValue == "pick" {
            let b = NSButton(checkboxWithTitle: it.name, target: self, action: #selector(toggle(_:)))
            b.tag = row
            b.state = it.taken ? .on : .off
            if let inside = it.inside {
                // Лежит внутри уже выбранной папки — отдельно брать нечего.
                b.isEnabled = false
                b.title = it.name + "  (внутри «\(inside)»)"
            }
            return b
        }
        let b = NSButton(title: "Внутрь ›", target: self, action: #selector(descend(_:)))
        b.bezelStyle = .inline
        b.tag = row
        b.isEnabled = it.inside == nil
        return b
    }
}

// --------------------------------------------------------------- приложение
final class App: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private var state: SyncState?
    private var watcher: Watcher?
    private var picker: PickerWindow?
    private let queue = DispatchQueue(label: "yandexsync.sync")
    private var syncing = false
    private var quietUntil = Date.distantPast
    private var lastFinderRun = Date.distantPast
    private var pendingEdit: DispatchWorkItem?
    private var netMonitor: NWPathMonitor?
    private var netWasDown = false

    func applicationDidFinishLaunching(_ n: Notification) {
        NSApp.setActivationPolicy(.accessory)
        let menu = NSMenu()
        menu.delegate = self
        item.menu = menu
        refresh(then: { self.startWatching() })
        hookEvents()
    }

    // ------------------------------------------------------------- значок
    private func setIcon(_ symbol: String, _ tip: String) {
        if let b = item.button {
            b.image = NSImage(systemSymbolName: symbol, accessibilityDescription: tip)
            b.image?.isTemplate = true
            b.toolTip = tip
        }
    }

    private func updateIcon() {
        if syncing { setIcon("arrow.triangle.2.circlepath", "Синхронизация…"); return }
        let s = state
        let conflicts = s?.conflicts.count ?? 0
        let bad = s?.folders.contains { ($0.lastResult ?? "") != "ok" } ?? false
        if conflicts > 0 {
            setIcon("exclamationmark.icloud", "Конфликтов: \(conflicts)")
        } else if bad {
            setIcon("xmark.icloud", "Последняя синхронизация не удалась")
        } else {
            setIcon("icloud", "Яндекс.Диск синхронизирован")
        }
    }

    private func refresh(then done: (() -> Void)? = nil) {
        DispatchQueue.global().async {
            let s = loadState()
            DispatchQueue.main.async {
                self.state = s
                self.updateIcon()
                done?()
            }
        }
    }

    // --------------------------------------------------------------- меню
    func menuWillOpen(_ menu: NSMenu) {
        rebuild(menu)
        refresh(then: { self.rebuild(menu) })
    }

    private func rebuild(_ menu: NSMenu) {
        menu.removeAllItems()
        let s = state

        if syncing {
            menu.addItem(disabled("Синхронизация…"))
        } else if let f = s?.folders, !f.isEmpty {
            let last = f.compactMap { $0.lastSync }.max()
            menu.addItem(disabled(last.map { "Синхронизировано: " + pretty($0) }
                                  ?? "Ещё ни разу не синхронизировано"))
        } else {
            menu.addItem(disabled("Ни одной папки не выбрано"))
        }
        menu.addItem(.separator())

        for f in s?.folders ?? [] {
            let mi = NSMenuItem(title: f.remote, action: #selector(syncOne(_:)), keyEquivalent: "")
            mi.target = self
            mi.representedObject = f.remote
            var note = "\(f.files) файл., \(mb(f.bytes))"
            switch f.lastResult ?? "" {
            case "ok": break
            case "needs-confirm": note = "остановлено страховкой — нажмите"
            case "timeout": note = "превышено время ожидания"
            case "error": note = "ошибка, смотрите журнал"
            default: note = "ещё не синхронизировалась"
            }
            if f.conflicts > 0 { note = "конфликтов: \(f.conflicts) — " + note }
            mi.attributedTitle = twoLine(f.remote, note, warn: f.conflicts > 0 ||
                                         (f.lastResult ?? "ok") != "ok")
            menu.addItem(mi)
        }
        if !(s?.folders.isEmpty ?? true) { menu.addItem(.separator()) }

        if let c = s?.conflicts, !c.isEmpty {
            let head = NSMenuItem(title: "Разрешить конфликты (\(c.count))", action: nil, keyEquivalent: "")
            let sub = NSMenu()
            for r in c {
                let mi = NSMenuItem(title: "\(r.folder) / \(r.file)",
                                    action: #selector(resolveOne(_:)), keyEquivalent: "")
                mi.target = self
                mi.representedObject = r.file
                sub.addItem(mi)
            }
            head.submenu = sub
            menu.addItem(head)
            menu.addItem(.separator())
        }

        if let bad = s?.folders.first(where: { $0.lastResult == "needs-confirm" }) {
            let mi = NSMenuItem(title: "Подтвердить массовое изменение…",
                                action: #selector(confirmForce(_:)), keyEquivalent: "")
            mi.target = self
            mi.representedObject = bad.remote
            menu.addItem(mi)
        }

        add(menu, "Выбрать папки…", #selector(openPicker))
        add(menu, "Синхронизировать всё", #selector(syncAll))
        menu.addItem(.separator())
        add(menu, "Открыть папку", #selector(openRoot))
        add(menu, "Журнал", #selector(openLog))
        let auto = NSMenuItem(title: "Запускать при входе", action: #selector(toggleAgent),
                              keyEquivalent: "")
        auto.target = self
        auto.state = FileManager.default.fileExists(atPath: AGENT) ? .on : .off
        menu.addItem(auto)
        menu.addItem(.separator())
        add(menu, "Выйти", #selector(quit))
    }

    private func add(_ m: NSMenu, _ title: String, _ sel: Selector) {
        let mi = NSMenuItem(title: title, action: sel, keyEquivalent: "")
        mi.target = self
        m.addItem(mi)
    }
    private func disabled(_ t: String) -> NSMenuItem {
        let mi = NSMenuItem(title: t, action: nil, keyEquivalent: "")
        mi.isEnabled = false
        return mi
    }
    private func twoLine(_ a: String, _ b: String, warn: Bool) -> NSAttributedString {
        let s = NSMutableAttributedString(string: a + "\n",
            attributes: [.font: NSFont.menuFont(ofSize: 0)])
        s.append(NSAttributedString(string: b, attributes: [
            .font: NSFont.menuFont(ofSize: NSFont.smallSystemFontSize),
            .foregroundColor: warn ? NSColor.systemOrange : NSColor.secondaryLabelColor]))
        return s
    }
    private func mb(_ b: Int) -> String {
        b > 1048576 ? String(format: "%.1f МБ", Double(b) / 1048576) : "\(b / 1024) КБ"
    }
    private func pretty(_ stamp: String) -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        guard let d = f.date(from: stamp) else { return stamp }
        let mins = Int(-d.timeIntervalSinceNow / 60)
        if mins < 1 { return "только что" }
        if mins < 60 { return "\(mins) мин назад" }
        let out = DateFormatter()
        out.dateFormat = Calendar.current.isDateInToday(d) ? "HH:mm" : "d MMMM, HH:mm"
        out.locale = Locale(identifier: "ru_RU")
        return out.string(from: d)
    }

    // ------------------------------------------------------------ действия
    @objc private func openPicker() {
        if picker == nil { picker = PickerWindow(onChanged: { self.refresh() }) }
        NSApp.activate(ignoringOtherApps: true)
        picker?.showWindow(nil)
        picker?.window?.makeKeyAndOrderFront(nil)
    }
    @objc private func syncAll() { sync(["--all"], reason: "вручную") }
    @objc private func syncOne(_ s: NSMenuItem) {
        guard let f = s.representedObject as? String else { return }
        sync([f], reason: "вручную")
    }
    @objc private func resolveOne(_ s: NSMenuItem) {
        guard let file = s.representedObject as? String else { return }
        DispatchQueue.global().async { yasync(["resolve", file]); self.refresh() }
    }
    @objc private func confirmForce(_ s: NSMenuItem) {
        guard let folder = s.representedObject as? String,
              let f = state?.folders.first(where: { $0.remote == folder }) else { return }
        let a = NSAlert()
        a.alertStyle = .warning
        a.messageText = "Синхронизация остановлена страховкой"
        a.informativeText = (f.lastReason ?? "Изменилось подозрительно много файлов.") +
            "\n\nЭто защита от массовой порчи: столько изменений обычно означает, " +
            "что папку случайно переместили или очистили. Продолжать стоит, только " +
            "если вы сами это сделали и понимаете, что будет применено."
        a.addButton(withTitle: "Отмена")
        a.addButton(withTitle: "Применить изменения")
        NSApp.activate(ignoringOtherApps: true)
        if a.runModal() == .alertSecondButtonReturn {
            sync([folder, "--force"], reason: "подтверждено вручную")
        }
    }
    @objc private func openRoot() {
        NSWorkspace.shared.open(URL(fileURLWithPath: state?.root ?? HOME + "/YandexDisk"))
    }
    @objc private func openLog() {
        NSWorkspace.shared.open(URL(fileURLWithPath: state?.log
            ?? HOME + "/Library/Logs/yandex-sync.log"))
    }
    @objc private func toggleAgent() {
        let fm = FileManager.default
        if fm.fileExists(atPath: AGENT) {
            _ = try? fm.removeItem(atPath: AGENT)
            shell("/bin/launchctl", ["bootout", "gui/\(getuid())/com.local.yandexsync"])
        } else {
            let exe = Bundle.main.executablePath ?? (HOME + "/bin/YandexSync")
            let plist = """
            <?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
            <plist version="1.0"><dict>
              <key>Label</key><string>com.local.yandexsync</string>
              <key>ProgramArguments</key><array><string>\(exe)</string></array>
              <key>RunAtLoad</key><true/>
            </dict></plist>
            """
            try? fm.createDirectory(atPath: (AGENT as NSString).deletingLastPathComponent,
                                    withIntermediateDirectories: true)
            try? plist.write(toFile: AGENT, atomically: true, encoding: .utf8)
            shell("/bin/launchctl", ["bootstrap", "gui/\(getuid())", AGENT])
        }
    }
    @objc private func quit() { NSApp.terminate(nil) }

    private func shell(_ exe: String, _ args: [String]) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        p.standardError = Pipe()
        try? p.run()
        p.waitUntilExit()
    }

    // ------------------------------------------------------------- запуск
    private func sync(_ args: [String], reason: String) {
        guard !syncing else { return }
        syncing = true
        updateIcon()
        queue.async {
            yasync(["sync"] + args)
            DispatchQueue.main.async {
                self.syncing = false
                // Мы сами только что писали в папку. Не принимаем собственное эхо
                // за правку пользователя, иначе синхронизация заведёт саму себя.
                self.quietUntil = Date().addingTimeInterval(SELF_ECHO)
                self.refresh()
            }
        }
    }

    private func startWatching() {
        let root = state?.root ?? HOME + "/YandexDisk"
        try? FileManager.default.createDirectory(atPath: root, withIntermediateDirectories: true)
        watcher = Watcher(path: root) { [weak self] paths in
            self?.filesChanged(paths)
        }
    }

    private func filesChanged(_ paths: [String]) {
        if syncing || Date() < quietUntil { return }
        // Обе версии конфликта создаём не мы, а rclone, и трогать их не нужно.
        let real = paths.contains { !$0.hasSuffix(".local") && !$0.hasSuffix(".remote")
                                    && !($0 as NSString).lastPathComponent.hasPrefix(".") }
        if !real { return }
        pendingEdit?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.sync(["--all"], reason: "правка файлов") }
        pendingEdit = work
        DispatchQueue.main.asyncAfter(deadline: .now() + EDIT_DEBOUNCE, execute: work)
    }

    private func hookEvents() {
        let wc = NSWorkspace.shared.notificationCenter
        wc.addObserver(forName: NSWorkspace.didWakeNotification,
                       object: nil, queue: .main) { [weak self] _ in
            self?.sync(["--all"], reason: "пробуждение")
        }
        wc.addObserver(forName: NSWorkspace.didActivateApplicationNotification,
                       object: nil, queue: .main) { [weak self] note in
            guard let self = self else { return }
            let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
            guard app?.bundleIdentifier == "com.apple.finder" else { return }
            // Отдельного события «папку открыли» в macOS нет. Активация Finder —
            // ближайшее, что есть, поэтому держим её на коротком поводке.
            guard Date().timeIntervalSince(self.lastFinderRun) > FINDER_THROTTLE else { return }
            self.lastFinderRun = Date()
            self.sync(["--all"], reason: "открыт Finder")
        }
        let mon = NWPathMonitor()
        mon.pathUpdateHandler = { [weak self] path in
            guard let self = self else { return }
            DispatchQueue.main.async {
                if path.status != .satisfied { self.netWasDown = true; return }
                if self.netWasDown {
                    self.netWasDown = false
                    self.sync(["--all"], reason: "сеть вернулась")
                }
            }
        }
        mon.start(queue: DispatchQueue.global(qos: .utility))
        netMonitor = mon
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.run()
