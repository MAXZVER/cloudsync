// Облачная синхронизация: приложение в области уведомлений Windows.
//
// Сборка (компилятор уже встроен в Windows, ставить нечего):
//   C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /target:winexe ^
//       /out:CloudSync.exe CloudSync.cs /r:System.Web.Extensions.dll
//
// Это близнец CloudSync.swift: вся работа в yasync.py, здесь только выбор
// хранилища и папок и решение, КОГДА запускать.
//
// Про батарею. Ни одного цикла опроса: приложение спит, пока система его не
// разбудит. Источники пробуждения те же четыре, что и на macOS:
//   1. FileSystemWatcher на корне — файл изменился локально;
//   2. проводник вышел на передний план (WinEventHook) — считаем, что папку
//      могли открыть; отдельного события «папку открыли» в Windows нет;
//   3. выход из сна — SystemEvents.PowerModeChanged;
//   4. вернулась сеть — NetworkChange.NetworkAvailabilityChanged.
// Таймеры используются только как однократная задержка, чтобы пачка
// сохранений стоила одной синхронизации, а не как опрос.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Net.NetworkInformation;
using System.Runtime.InteropServices;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

static class Paths {
    public static readonly string Home =
        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
    public static readonly string Local =
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
    public static string Support { get { return Path.Combine(Local, "CloudSync"); } }

    // yasync.py ищем рядом с собой, потом в каталоге состояния.
    public static string Engine() {
        string near = Path.Combine(
            Path.GetDirectoryName(Application.ExecutablePath), "yasync.py");
        if (File.Exists(near)) return near;
        return Path.Combine(Support, "bin", "yasync.py");
    }
}

static class Py {
    static string exe;
    static string pre = "";

    static bool Works(string file, string prefix) {
        try {
            var psi = new ProcessStartInfo(file, (prefix + " --version").Trim());
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            using (var p = Process.Start(psi)) { p.WaitForExit(5000); return p.ExitCode == 0; }
        } catch { return false; }
    }

    public static string Find() {
        if (exe != null) return exe;
        // py.exe — штатный лаунчер Python на Windows, он же выберет версию.
        if (Works("py", "-3")) { exe = "py"; pre = "-3 "; }
        else if (Works("python", "")) { exe = "python"; pre = ""; }
        else exe = "";
        return exe;
    }

    static string Quote(string s) { return "\"" + s.Replace("\"", "\\\"") + "\""; }

    /// Запуск yasync.py. Консольное окно не показывается никогда.
    public static string Run(params string[] args) {
        if (Find() == "") return "";
        var line = pre + Quote(Paths.Engine());
        foreach (var a in args) line += " " + Quote(a);
        var psi = new ProcessStartInfo(exe, line);
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        psi.StandardOutputEncoding = System.Text.Encoding.UTF8;
        psi.StandardErrorEncoding = System.Text.Encoding.UTF8;
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        try {
            using (var p = Process.Start(psi)) {
                string outp = p.StandardOutput.ReadToEnd();
                p.StandardError.ReadToEnd();
                p.WaitForExit();
                return outp;
            }
        } catch { return ""; }
    }
}

class FolderState {
    public string Remote = "", Local = "", LastSync, LastResult, LastReason;
    public int Files, Conflicts;
    public long Bytes;
}
class ConflictRef { public string Folder = "", File = ""; }
class RemoteEntry { public string Remote = "", Type = "", Label = ""; }

class State {
    public string Root = "", Log = "", Remote = "", Label = "", Rclone = "";
    public bool RcloneFound;
    public List<FolderState> Folders = new List<FolderState>();
    public List<ConflictRef> Conflicts = new List<ConflictRef>();

    static string S(Dictionary<string, object> d, string k) {
        object v; return d.TryGetValue(k, out v) && v != null ? v.ToString() : null;
    }
    static int I(Dictionary<string, object> d, string k) {
        object v; return d.TryGetValue(k, out v) && v != null ? Convert.ToInt32(v) : 0;
    }

    public static State Load() {
        string json = Py.Run("state");
        if (string.IsNullOrEmpty(json)) return null;
        try {
            var ser = new JavaScriptSerializer();
            var d = ser.Deserialize<Dictionary<string, object>>(json);
            var st = new State();
            st.Root = S(d, "root") ?? "";
            st.Log = S(d, "log") ?? "";
            st.Remote = S(d, "remote") ?? "";
            st.Label = S(d, "label") ?? "";
            st.Rclone = S(d, "rclone") ?? "";
            object rf; st.RcloneFound = d.TryGetValue("rcloneFound", out rf) && rf is bool && (bool)rf;
            foreach (var o in (object[])d["folders"]) {
                var f = (Dictionary<string, object>)o;
                st.Folders.Add(new FolderState {
                    Remote = S(f, "remote") ?? "", Local = S(f, "local") ?? "",
                    LastSync = S(f, "lastSync"), LastResult = S(f, "lastResult"),
                    LastReason = S(f, "lastReason"),
                    Files = I(f, "files"), Bytes = Convert.ToInt64(f["bytes"]),
                    Conflicts = I(f, "conflicts"),
                });
            }
            foreach (var o in (object[])d["conflicts"]) {
                var c = (Dictionary<string, object>)o;
                st.Conflicts.Add(new ConflictRef { Folder = S(c, "folder") ?? "",
                                                   File = S(c, "file") ?? "" });
            }
            return st;
        } catch { return null; }
    }

    public static List<RemoteEntry> Remotes() {
        var list = new List<RemoteEntry>();
        string json = Py.Run("remotes", "--json");
        if (string.IsNullOrEmpty(json)) return list;
        try {
            var arr = new JavaScriptSerializer().Deserialize<object[]>(json);
            foreach (var o in arr) {
                var d = (Dictionary<string, object>)o;
                list.Add(new RemoteEntry { Remote = S(d, "remote") ?? "",
                                           Type = S(d, "type") ?? "",
                                           Label = S(d, "label") ?? "" });
            }
        } catch { }
        return list;
    }
}

/// Значок рисуем кодом: файлов-ресурсов нет, приложение остаётся одним файлом.
static class Glyph {
    public static Icon Make(Color dot) {
        using (var bmp = new Bitmap(16, 16))
        using (var g = Graphics.FromImage(bmp)) {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.Clear(Color.Transparent);
            using (var b = new SolidBrush(Color.FromArgb(235, 240, 245))) {
                g.FillEllipse(b, 1, 6, 7, 7);
                g.FillEllipse(b, 5, 3, 8, 8);
                g.FillEllipse(b, 9, 7, 6, 6);
                g.FillRectangle(b, 4, 9, 9, 4);
            }
            if (dot != Color.Empty) {
                using (var b = new SolidBrush(dot)) g.FillEllipse(b, 9, 9, 7, 7);
                using (var p = new Pen(Color.FromArgb(30, 30, 30), 1f))
                    g.DrawEllipse(p, 9, 9, 7, 7);
            }
            return Icon.FromHandle(bmp.GetHicon());
        }
    }
}

class Tray : ApplicationContext {
    // Проводник открывают часто, а синхронизация — сетевой запрос.
    const int ExplorerThrottleSec = 120;
    const int EditDebounceMs = 10000;
    const int SelfEchoSec = 5;        // наша же запись поднимает watcher

    readonly NotifyIcon icon = new NotifyIcon();
    // Носитель для возврата в поток интерфейса. ContextMenuStrip для этого не
    // годится: пока меню ни разу не открывали, у него нет оконного дескриптора,
    // и BeginInvoke падает с InvalidOperationException.
    readonly Control ui = new Control();
    readonly System.Windows.Forms.Timer debounce = new System.Windows.Forms.Timer();
    FileSystemWatcher watcher;
    State state;
    List<RemoteEntry> remotes;
    bool syncing;
    DateTime quietUntil = DateTime.MinValue;
    DateTime lastExplorer = DateTime.MinValue;
    bool netWasDown;
    Icon current;

    // WinEventHook: узнаём, что на передний план вышло другое окно.
    delegate void WinEventProc(IntPtr hook, uint ev, IntPtr hwnd, int idObject,
                               int idChild, uint thread, uint time);
    [DllImport("user32.dll")]
    static extern IntPtr SetWinEventHook(uint min, uint max, IntPtr mod, WinEventProc proc,
                                         uint pid, uint thread, uint flags);
    [DllImport("user32.dll")]
    static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    WinEventProc hookProc;          // держим ссылку, иначе сборщик мусора её съест

    public Tray() {
        ui.CreateControl();
        IntPtr forceHandle = ui.Handle;     // дескриптор создаётся именно здесь
        GC.KeepAlive(forceHandle);
        icon.ContextMenuStrip = new ContextMenuStrip();
        icon.ContextMenuStrip.Opening += (s, e) => { Rebuild(); RefreshAsync(); };
        icon.MouseUp += (s, e) => { if (e.Button == MouseButtons.Left) ShowMenu(); };
        icon.Visible = true;
        SetIcon();
        RefreshAsync(StartWatching);

        debounce.Interval = EditDebounceMs;
        debounce.Tick += (s, e) => { debounce.Stop(); Sync("--all"); };

        SystemEvents.PowerModeChanged += (s, e) => {
            if (e.Mode == PowerModes.Resume) Sync("--all");
        };
        NetworkChange.NetworkAvailabilityChanged += (s, e) => {
            if (!e.IsAvailable) { netWasDown = true; return; }
            if (netWasDown) { netWasDown = false; Sync("--all"); }
        };
        hookProc = OnForeground;
        SetWinEventHook(0x0003, 0x0003, IntPtr.Zero, hookProc, 0, 0, 0);  // EVENT_SYSTEM_FOREGROUND
    }

    void OnForeground(IntPtr hook, uint ev, IntPtr hwnd, int idObject, int idChild,
                      uint thread, uint time) {
        if (hwnd == IntPtr.Zero) return;
        uint pid;
        GetWindowThreadProcessId(hwnd, out pid);
        string name;
        try { name = Process.GetProcessById((int)pid).ProcessName; } catch { return; }
        if (!name.Equals("explorer", StringComparison.OrdinalIgnoreCase)) return;
        // Отдельного события «папку открыли» в Windows нет. Выход проводника на
        // передний план — ближайшее, что есть, поэтому держим его на поводке.
        if ((DateTime.Now - lastExplorer).TotalSeconds < ExplorerThrottleSec) return;
        lastExplorer = DateTime.Now;
        Sync("--all");
    }

    void ShowMenu() {
        Rebuild();
        var mi = typeof(NotifyIcon).GetMethod("ShowContextMenu",
            System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
        if (mi != null) mi.Invoke(icon, null);
        RefreshAsync();
    }

    void SetIcon() {
        Color dot = Color.Empty;
        string tip;
        if (syncing) { dot = Color.FromArgb(53, 116, 240); tip = "Синхронизация…"; }
        else if (Py.Find() == "") { dot = Color.FromArgb(200, 60, 60); tip = "Python не найден"; }
        else if (state == null || state.Remote == "") {
            dot = Color.FromArgb(150, 150, 150); tip = "Хранилище не выбрано";
        } else if (!state.RcloneFound) {
            dot = Color.FromArgb(200, 60, 60); tip = "rclone не найден: " + state.Rclone;
        } else if (state.Conflicts.Count > 0) {
            dot = Color.FromArgb(235, 150, 40);
            tip = "Конфликтов: " + state.Conflicts.Count;
        } else {
            bool bad = false;
            foreach (var f in state.Folders) if (f.LastResult != "ok") bad = true;
            if (bad) { dot = Color.FromArgb(200, 60, 60); tip = "Последняя синхронизация не удалась"; }
            else tip = state.Label + " синхронизировано";
        }
        var made = Glyph.Make(dot);
        icon.Icon = made;
        if (current != null) current.Dispose();
        current = made;
        icon.Text = tip.Length > 63 ? tip.Substring(0, 60) + "…" : tip;
    }

    /// Вернуться в поток интерфейса. Если дескриптора ещё нет, делать нечего.
    void Post(Delegate d) {
        if (ui.IsHandleCreated) { try { ui.BeginInvoke(d); } catch (InvalidOperationException) { } }
    }

    void RefreshAsync() { RefreshAsync(null); }
    void RefreshAsync(Action then) {
        var t = new System.Threading.Thread(() => {
            var s = State.Load();
            Post((Action)(() => {
                state = s; SetIcon();
                if (then != null) then();
            }));
        });
        t.IsBackground = true;
        t.Start();
    }

    // ------------------------------------------------------------------ меню
    ToolStripMenuItem Item(string text, EventHandler on) {
        var mi = new ToolStripMenuItem(text);
        if (on != null) mi.Click += on; else mi.Enabled = false;
        return mi;
    }

    string Pretty(string stamp) {
        DateTime d;
        if (!DateTime.TryParse(stamp, out d)) return stamp;
        var mins = (int)(DateTime.Now - d).TotalMinutes;
        if (mins < 1) return "только что";
        if (mins < 60) return mins + " мин назад";
        return d.ToString(d.Date == DateTime.Today ? "HH:mm" : "d MMMM, HH:mm");
    }
    string Mb(long b) {
        return b > 1048576 ? (b / 1048576.0).ToString("0.0") + " МБ" : (b / 1024) + " КБ";
    }

    void Rebuild() {
        var m = icon.ContextMenuStrip;
        m.Items.Clear();
        var s = state;

        if (Py.Find() == "") {
            m.Items.Add(Item("Python не найден — движок запустить нечем", null));
            m.Items.Add(new ToolStripSeparator());
            m.Items.Add(Item("Выход", (a, b) => Quit()));
            return;
        }
        if (syncing) m.Items.Add(Item("Синхронизация…", null));
        else if (s != null && s.Folders.Count > 0) {
            string last = null;
            foreach (var f in s.Folders)
                if (f.LastSync != null && (last == null || String.CompareOrdinal(f.LastSync, last) > 0))
                    last = f.LastSync;
            m.Items.Add(Item(last != null ? "Синхронизировано: " + Pretty(last)
                                          : "Ещё ни разу не синхронизировано", null));
        } else {
            m.Items.Add(Item(s == null || s.Remote == "" ? "Хранилище не выбрано"
                                                         : "Ни одной папки не выбрано", null));
        }
        if (s != null && s.Remote != "" && !s.RcloneFound)
            m.Items.Add(Item("rclone не найден: " + s.Rclone, null));
        m.Items.Add(new ToolStripSeparator());

        if (s != null) {
            foreach (var f in s.Folders) {
                var folder = f;
                string note = f.Files + " файл., " + Mb(f.Bytes);
                if (f.LastResult == "needs-confirm") note = "остановлено страховкой — нажмите";
                else if (f.LastResult == "timeout") note = "превышено время ожидания";
                else if (f.LastResult == "error") note = "ошибка, смотрите журнал";
                else if (f.LastResult != "ok") note = "ещё не синхронизировалась";
                if (f.Conflicts > 0) note = "конфликтов: " + f.Conflicts + " — " + note;
                var mi = Item(f.Remote + "  —  " + note, (a, b) => Sync(folder.Remote));
                mi.ToolTipText = f.Local;
                m.Items.Add(mi);
            }
            if (s.Folders.Count > 0) m.Items.Add(new ToolStripSeparator());

            if (s.Conflicts.Count > 0) {
                var head = new ToolStripMenuItem("Разрешить конфликты (" + s.Conflicts.Count + ")");
                foreach (var c in s.Conflicts) {
                    var file = c.File;
                    head.DropDownItems.Add(Item(c.Folder + " / " + c.File,
                                                (a, b) => Resolve(file)));
                }
                m.Items.Add(head);
                m.Items.Add(new ToolStripSeparator());
            }
            foreach (var f in s.Folders) {
                if (f.LastResult != "needs-confirm") continue;
                var folder = f;
                m.Items.Add(Item("Подтвердить массовое изменение…", (a, b) => ConfirmForce(folder)));
                break;
            }
        }

        var store = new ToolStripMenuItem(
            s != null && s.Label != "" ? "Хранилище: " + s.Label : "Выбрать хранилище…");
        if (remotes == null) remotes = State.Remotes();
        if (remotes.Count == 0) {
            store.DropDownItems.Add(Item("rclone не знает ни одного хранилища", null));
            store.DropDownItems.Add(Item("Заведите его командой  rclone config", null));
        }
        foreach (var r in remotes) {
            var name = r.Remote;
            var mi = Item(r.Label + "  (" + r.Remote + ")", (a, b) => PickRemote(name));
            mi.Checked = (s != null && r.Remote == s.Remote);
            store.DropDownItems.Add(mi);
        }
        m.Items.Add(store);

        var pick = Item("Выбрать папки…", (a, b) => OpenPicker());
        pick.Enabled = s != null && s.Remote != "";
        m.Items.Add(pick);
        m.Items.Add(Item("Синхронизировать всё", (a, b) => Sync("--all")));
        m.Items.Add(new ToolStripSeparator());
        m.Items.Add(Item("Открыть папку", (a, b) => {
            if (s != null && Directory.Exists(s.Root)) Process.Start("explorer.exe", s.Root);
        }));
        m.Items.Add(Item("Журнал", (a, b) => {
            if (s != null && File.Exists(s.Log)) Process.Start("notepad.exe", s.Log);
        }));
        var auto = Item("Запускать при входе", (a, b) => ToggleAutostart());
        auto.Checked = AutostartOn();
        m.Items.Add(auto);
        m.Items.Add(new ToolStripSeparator());
        m.Items.Add(Item("Выход", (a, b) => Quit()));
    }

    // --------------------------------------------------------------- действия
    void Sync(params string[] args) {
        if (syncing) return;
        syncing = true; SetIcon();
        var t = new System.Threading.Thread(() => {
            var list = new List<string>(); list.Add("sync"); list.AddRange(args);
            Py.Run(list.ToArray());
            Post((Action)(() => {
                syncing = false;
                // Мы сами только что писали в папку. Своё эхо за правку не принимаем,
                // иначе синхронизация заведёт саму себя.
                quietUntil = DateTime.Now.AddSeconds(SelfEchoSec);
                RefreshAsync();
            }));
        });
        t.IsBackground = true; t.Start();
    }

    void Resolve(string file) {
        var t = new System.Threading.Thread(() => {
            Py.Run("resolve", file);
            Post((Action)(() => RefreshAsync()));
        });
        t.IsBackground = true; t.Start();
    }

    void ConfirmForce(FolderState f) {
        string why = f.LastReason ?? "Изменилось подозрительно много файлов.";
        var r = MessageBox.Show(
            why + "\n\nЭто защита от массовой порчи: столько изменений обычно означает, "
                + "что папку случайно переместили или очистили. Продолжать стоит, только если "
                + "вы сами это сделали и понимаете, что будет применено.",
            "Синхронизация остановлена страховкой",
            MessageBoxButtons.OKCancel, MessageBoxIcon.Warning);
        if (r == DialogResult.OK) Sync(f.Remote, "--force");
    }

    void PickRemote(string remote) {
        if (syncing) return;
        syncing = true; SetIcon();
        var t = new System.Threading.Thread(() => {
            Py.Run("init", remote);
            Post((Action)(() => {
                syncing = false; remotes = null;
                RefreshAsync(StartWatching);
            }));
        });
        t.IsBackground = true; t.Start();
    }

    void OpenPicker() {
        if (state == null || state.Remote == "") return;
        using (var f = new PickerForm(state.Label, state.Root)) f.ShowDialog();
        remotes = null;
        RefreshAsync(StartWatching);
    }

    // ------------------------------------------------------------ автозапуск
    const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    const string RunName = "CloudSync";
    bool AutostartOn() {
        using (var k = Registry.CurrentUser.OpenSubKey(RunKey))
            return k != null && k.GetValue(RunName) != null;
    }
    void ToggleAutostart() {
        using (var k = Registry.CurrentUser.OpenSubKey(RunKey, true)) {
            if (k == null) return;
            if (AutostartOn()) k.DeleteValue(RunName, false);
            else k.SetValue(RunName, "\"" + Application.ExecutablePath + "\"");
        }
    }

    // ------------------------------------------------------------ наблюдение
    void StartWatching() {
        if (watcher != null) { watcher.Dispose(); watcher = null; }
        if (state == null || string.IsNullOrEmpty(state.Root)) return;
        try { Directory.CreateDirectory(state.Root); } catch { return; }
        watcher = new FileSystemWatcher(state.Root);
        watcher.IncludeSubdirectories = true;
        watcher.NotifyFilter = NotifyFilters.LastWrite | NotifyFilters.FileName
                             | NotifyFilters.DirectoryName | NotifyFilters.Size;
        FileSystemEventHandler h = (s, e) => OnChanged(e.Name);
        watcher.Changed += h; watcher.Created += h; watcher.Deleted += h;
        watcher.Renamed += (s, e) => OnChanged(e.Name);
        watcher.SynchronizingObject = ui;
        watcher.EnableRaisingEvents = true;
    }

    void OnChanged(string name) {
        if (syncing || DateTime.Now < quietUntil) return;
        if (name == null) return;
        // Обе версии конфликта создаёт rclone, трогать их незачем.
        if (name.EndsWith(".local") || name.EndsWith(".remote")) return;
        if (Path.GetFileName(name).StartsWith(".")) return;
        debounce.Stop(); debounce.Start();
    }

    void Quit() {
        icon.Visible = false;
        if (watcher != null) watcher.Dispose();
        ExitThread();
    }
}

/// Выбор папок: список с галочками и проход вглубь хранилища.
class PickerForm : Form {
    readonly ListView list = new ListView();
    readonly Label pathLabel = new Label();
    readonly Button up = new Button();
    string path = "";
    readonly string store;
    bool busy;

    class Item { public string Name, Path, Inside; public bool Taken; }
    List<Item> items = new List<Item>();

    public PickerForm(string storeName, string root) {
        store = storeName;
        Text = "Папки: " + storeName;
        Width = 560; Height = 480;
        StartPosition = FormStartPosition.CenterScreen;

        up.Text = "Наверх"; up.Left = 12; up.Top = 10; up.Width = 80;
        up.Click += (s, e) => {
            if (path == "") return;
            int i = path.LastIndexOf('/');
            LoadPath(i < 0 ? "" : path.Substring(0, i));
        };
        pathLabel.Left = 100; pathLabel.Top = 15; pathLabel.Width = 420;
        pathLabel.AutoEllipsis = true;

        list.Left = 12; list.Top = 44; list.Width = 520; list.Height = 330;
        list.View = View.Details; list.CheckBoxes = true; list.FullRowSelect = true;
        list.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Bottom;
        list.Columns.Add("Синхронизировать", 380);
        list.Columns.Add("", 120);
        list.ItemChecked += OnChecked;
        list.DoubleClick += (s, e) => {
            if (list.SelectedIndices.Count == 0) return;
            var it = items[list.SelectedIndices[0]];
            if (it.Inside == null) LoadPath(it.Path);
        };

        var hint = new Label();
        hint.Left = 12; hint.Top = 382; hint.Width = 520; hint.Height = 52;
        hint.Anchor = AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Bottom;
        hint.Text = "Галочка — папка синхронизируется целиком в " + root + ". "
                  + "Первая синхронизация может занять время. Снятие галочки останавливает "
                  + "синхронизацию, локальные файлы остаются на месте. "
                  + "Двойной клик — заглянуть внутрь.";

        Controls.Add(up); Controls.Add(pathLabel); Controls.Add(list); Controls.Add(hint);
        LoadPath("");
    }

    void LoadPath(string p) {
        path = p;
        pathLabel.Text = p == "" ? store : store + " / " + p;
        busy = true;
        list.Items.Clear();
        list.Items.Add(new ListViewItem("загружаю…"));
        var t = new System.Threading.Thread(() => {
            string json = Py.Run("ls", p, "--json");
            var got = new List<Item>();
            string err = null;
            try {
                var d = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(json);
                object e;
                if (d.TryGetValue("error", out e) && e != null) err = e.ToString();
                object arr;
                if (d.TryGetValue("items", out arr) && arr != null) {
                    foreach (var o in (object[])arr) {
                        var i = (Dictionary<string, object>)o;
                        object ins; i.TryGetValue("inside", out ins);
                        got.Add(new Item {
                            Name = i["name"].ToString(), Path = i["path"].ToString(),
                            Taken = Convert.ToBoolean(i["taken"]),
                            Inside = ins == null ? null : ins.ToString(),
                        });
                    }
                }
            } catch { err = "не разобрать ответ"; }
            BeginInvoke((Action)(() => {
                if (err != null) pathLabel.Text = "Не удалось прочитать хранилище: " + err;
                items = got;
                list.Items.Clear();
                foreach (var it in got) {
                    var lvi = new ListViewItem(
                        it.Inside == null ? it.Name : it.Name + "  (внутри «" + it.Inside + "»)");
                    lvi.Checked = it.Taken;
                    lvi.SubItems.Add(it.Inside == null ? "внутрь ⏎⏎" : "");
                    if (it.Inside != null) lvi.ForeColor = SystemColors.GrayText;
                    list.Items.Add(lvi);
                }
                busy = false;
            }));
        });
        t.IsBackground = true; t.Start();
    }

    void OnChecked(object sender, ItemCheckedEventArgs e) {
        if (busy || e.Item.Index >= items.Count) return;
        var it = items[e.Item.Index];
        if (it.Inside != null) { busy = true; e.Item.Checked = false; busy = false; return; }
        if (e.Item.Checked == it.Taken) return;
        bool add = e.Item.Checked;
        Enabled = false;
        var t = new System.Threading.Thread(() => {
            Py.Run(add ? "add" : "rm", it.Path);
            BeginInvoke((Action)(() => { Enabled = true; LoadPath(path); }));
        });
        t.IsBackground = true; t.Start();
    }
}

static class Program {
    [STAThread]
    static void Main() {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new Tray());
    }
}
