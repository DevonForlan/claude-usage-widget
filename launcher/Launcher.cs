// Standalone, precompiled launcher for the Claude Code Usage widget.
//
// Why this exists instead of launch.ps1: PowerShell's own process startup
// (~600ms) plus recompiling the same inline C# via Add-Type on every
// invocation (~940ms, measured directly) made "wake up the already-running
// widget" nearly as slow as a cold start - defeating the point. A precompiled
// .exe like this one starts in a few milliseconds instead.
//
// Behaviour: if a window titled "Claude Code Usage" already exists (owned by
// pythonw.exe/python.exe - the widget hides rather than quits on close, see
// widget.py's closeEvent), restore and force it to the foreground. Otherwise
// cold-start the widget - this only happens once per login in practice.
//
// Rebuild after editing:
//   & "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:winexe /out:UsageWidgetLauncher.exe Launcher.cs

using System;
using System.Diagnostics;
using System.IO;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

class Launcher
{
    private const string TargetTitle = "Claude Code Usage";

    // "pyw" is the Python Launcher for Windows' no-console entry point - it
    // resolves whichever Python 3 install is registered, so this doesn't need
    // to know any one machine's install path.
    private const string PythonExe = "pyw";

    // This launcher lives at <project>\launcher\UsageWidgetLauncher.exe, so
    // its own location is enough to find the project root without hardcoding
    // any machine- or account-specific path.
    private static readonly string ProjectDir =
        Path.GetDirectoryName(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location));

    // Must match claude_usage_widget/widget.py's WAKE_PORT. Connecting here
    // (rather than calling ShowWindow/SetForegroundWindow on the native
    // handle ourselves) lets the already-running Python process bring itself
    // to the front through Qt's own show()/activateWindow() path - see the
    // comment above WAKE_PORT in widget.py for why that distinction matters
    // (a raw external ShowWindow call left Qt's own input routing desynced:
    // painting and opacity still worked, but dragging, the close button, and
    // the opacity slider all stopped responding to clicks). A loopback TCP
    // connect is a push notification Qt reacts to on its next event-loop
    // turn - no polling interval to wait out, unlike an earlier flag-file
    // version of this.
    private const int WakePort = 51823;

    private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] private static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll")] private static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
    [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] private static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
    [DllImport("user32.dll")] private static extern void SwitchToThisWindow(IntPtr hWnd, bool fAltTab);
    [DllImport("user32.dll")] private static extern bool BringWindowToTop(IntPtr hWnd);

    private static IntPtr _found = IntPtr.Zero;

    private static bool EnumCallback(IntPtr hWnd, IntPtr lParam)
    {
        int len = GetWindowTextLength(hWnd);
        if (len == 0) return true;

        StringBuilder sb = new StringBuilder(len + 1);
        GetWindowText(hWnd, sb, sb.Capacity);
        if (sb.ToString() != TargetTitle) return true;

        uint procId;
        GetWindowThreadProcessId(hWnd, out procId);
        try
        {
            string name = Process.GetProcessById((int)procId).ProcessName;
            if (name.Equals("pythonw", StringComparison.OrdinalIgnoreCase) ||
                name.Equals("python", StringComparison.OrdinalIgnoreCase))
            {
                _found = hWnd;
                return false; // stop enumerating
            }
        }
        catch
        {
            // Process disappeared between enumeration and lookup - ignore.
        }
        return true;
    }

    private static void Main()
    {
        EnumWindows(EnumCallback, IntPtr.Zero);

        if (_found != IntPtr.Zero)
        {
            WakeUp(_found);
        }
        else
        {
            var psi = new ProcessStartInfo
            {
                FileName = PythonExe,
                Arguments = "-m claude_usage_widget",
                WorkingDirectory = ProjectDir,
                UseShellExecute = false,
            };
            Process.Start(psi);
        }
    }

    private static void WakeUp(IntPtr hwnd)
    {
        try
        {
            using (var client = new TcpClient())
            {
                client.Connect("127.0.0.1", WakePort);
            }
        }
        catch (SocketException)
        {
            // Widget process is there but not listening yet (e.g. still
            // starting up) - the foreground-steal retry loop below will
            // just keep trying regardless.
        }

        if (GetForegroundWindow() == hwnd) return;

        for (int attempt = 0; attempt < 4; attempt++)
        {
            // Tap Alt so Windows treats us as having just received input
            // (bypasses the foreground-lock restriction on this machine).
            keybd_event(0x12, 0, 0, UIntPtr.Zero);
            keybd_event(0x12, 0, 2, UIntPtr.Zero);

            IntPtr fgWindow = GetForegroundWindow();
            uint fgProcId;
            uint fgThread = GetWindowThreadProcessId(fgWindow, out fgProcId);
            uint curThread = GetCurrentThreadId();
            AttachThreadInput(fgThread, curThread, true);

            BringWindowToTop(hwnd);
            SwitchToThisWindow(hwnd, true);
            SetForegroundWindow(hwnd);

            AttachThreadInput(fgThread, curThread, false);

            // Check right away - success doesn't need to wait for a full
            // settle period before being noticed. Only sleep before a retry.
            if (GetForegroundWindow() == hwnd) return;

            Thread.Sleep(40);
        }
    }
}
