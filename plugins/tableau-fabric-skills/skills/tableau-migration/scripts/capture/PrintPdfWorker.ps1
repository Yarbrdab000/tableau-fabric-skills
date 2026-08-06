param(
    [Parameter(Mandatory = $true)][string]$Twbx,
    [Parameter(Mandatory = $true)][string]$OutDir,
    [Parameter(Mandatory = $true)][string]$LogPath,
    [string]$TableauExe = "C:\Program Files\Tableau\Tableau 2026.2\bin\tableau.exe"
)

$ErrorActionPreference = 'Continue'
$script:T0 = Get-Date
function Log([string]$m) {
    $el = "{0,7:N1}s" -f ((Get-Date) - $script:T0).TotalSeconds
    Add-Content -Path $LogPath -Value "[$el] $m" -Encoding UTF8
}

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class Nat {
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr h, EnumProc cb, IntPtr l);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessageW(IntPtr h, uint m, IntPtr w, string l);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessageW(IntPtr h, uint m, IntPtr w, StringBuilder l);
    [DllImport("user32.dll")] public static extern int GetDlgCtrlID(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr h);

    // Fast, correct child walk. The PowerShell delegate version was both buggy (shared
    // recursion state) and slow enough (~5s on a modern file dialog) that the Save dialog
    // was being torn down before we could drive it.
    public class W { public IntPtr H; public string Cls; public string Txt; public int Depth; }
    public static List<W> Walk(IntPtr root, int maxDepth) {
        var outp = new List<W>();
        var cur = new List<IntPtr>(); cur.Add(root);
        for (int d = 0; d <= maxDepth && cur.Count > 0; d++) {
            var next = new List<IntPtr>();
            foreach (var p in cur) {
                var lvl = new List<IntPtr>();
                EnumChildWindows(p, delegate(IntPtr h, IntPtr l) { lvl.Add(h); return true; }, IntPtr.Zero);
                foreach (var h in lvl) {
                    var c = new StringBuilder(256); GetClassNameW(h, c, 256);
                    var t = new StringBuilder(512); GetWindowTextW(h, t, 512);
                    outp.Add(new W { H = h, Cls = c.ToString(), Txt = t.ToString(), Depth = d });
                    next.Add(h);
                }
            }
            cur = next;
        }
        return outp;
    }
}
"@

$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$TC = [System.Windows.Automation.Condition]::TrueCondition
$script:TabPid = 0

# Park off-screen. Minimising is NOT usable: Qt refuses to materialise menus/dialogs on an
# iconic window. Off-screen keeps Qt fully normal while staying invisible to the user.
function Move-OffScreen([IntPtr]$h) {
    [void][Nat]::ShowWindow($h, 9)
    Start-Sleep -Milliseconds 500
    [void][Nat]::SetWindowPos($h, [IntPtr]::Zero, -3200, -2400, 1920, 1200, 0x0014)  # NOACTIVATE|NOZORDER
    Start-Sleep -Milliseconds 300
}

function Get-TabWindows {
    $script:found = @()
    $cb = [Nat+EnumProc] {
        param($h, $l)
        $p2 = 0
        [void][Nat]::GetWindowThreadProcessId($h, [ref]$p2)
        if ($p2 -eq $script:TabPid) {
            $sb = New-Object Text.StringBuilder 512
            [void][Nat]::GetWindowTextW($h, $sb, 512)
            $t = $sb.ToString()
            $cb2 = New-Object Text.StringBuilder 256
            [void][Nat]::GetClassNameW($h, $cb2, 256)
            $c = $cb2.ToString()
            if ($t -and $c -notlike 'SysShadow*' -and $c -notlike '*DropShadow*') {
                $script:found += [pscustomobject]@{ H = $h; Title = $t; Class = $c }
            }
        }
        return $true
    }
    [void][Nat]::EnumWindows($cb, [IntPtr]::Zero)
    return $script:found
}

function Get-MainHwnd {
    $w = Get-TabWindows | Where-Object { $_.Class -like '*QWindowIcon' } | Select-Object -First 1
    if ($w) { return $w.H } else { return [IntPtr]::Zero }
}

# Get-TabWindows drops untitled windows; a modal shutdown prompt may have no title, so during
# close we enumerate every visible top-level window the process owns.
function Get-CloseWindows {
    $script:cfound = @()
    $cb = [Nat+EnumProc] {
        param($h, $l)
        $p2 = 0
        [void][Nat]::GetWindowThreadProcessId($h, [ref]$p2)
        if ($p2 -eq $script:TabPid -and [Nat]::IsWindowVisible($h)) {
            $sb = New-Object Text.StringBuilder 512
            [void][Nat]::GetWindowTextW($h, $sb, 512)
            $cb2 = New-Object Text.StringBuilder 256
            [void][Nat]::GetClassNameW($h, $cb2, 256)
            $c = $cb2.ToString()
            if ($c -notlike 'SysShadow*' -and $c -notlike '*DropShadow*' -and $c -notlike 'IME' -and $c -notlike 'MSCTFIME*') {
                $script:cfound += [pscustomobject]@{ H = $h; Title = $sb.ToString(); Class = $c }
            }
        }
        return $true
    }
    [void][Nat]::EnumWindows($cb, [IntPtr]::Zero)
    return $script:cfound
}

# Press a discard/dismiss button on a shutdown prompt. Deliberately an ALLOW-LIST: anything
# that would persist a change ("Save", "Yes", "Overwrite") is never pressed, so an unexpected
# prompt stalls the close instead of silently modifying the customer's workbook.
$script:DiscardLabels = @("Don't Save", "Do&n't Save", "Don't save", "Discard", "&Discard",
                          "No", "&No", "Close Without Saving", "Don't Restore")
function Invoke-DiscardButton([IntPtr]$hwnd) {
    foreach ($c in [Nat]::Walk($hwnd, 6)) {
        if ($c.Cls -notlike '*Button*' -and $c.Cls -ne 'Button') { continue }
        $label = ($c.Txt -replace '&', '').Trim()
        foreach ($want in $script:DiscardLabels) {
            if ($label -eq ($want -replace '&', '')) {
                [void][Nat]::PostMessage($c.H, 0x00F5, [IntPtr]::Zero, [IntPtr]::Zero)  # BM_CLICK
                return $label
            }
        }
    }
    return $null
}
function Get-Root {
    $h = Get-MainHwnd
    if ($h -eq [IntPtr]::Zero) { return $null }
    return $AE::FromHandle($h)
}
function Expand-El($e) { try { $e.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Expand(); return $true } catch { return $false } }
function Collapse-El($e) { try { $e.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Collapse() } catch {} }
function Invoke-El($e) { $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }

function Get-MenuBar($root) {
    foreach ($k in $root.FindAll($TS::Children, $TC)) {
        if ($k.Current.ControlType.ProgrammaticName -match 'MenuBar') { return $k }
    }
    return $null
}
# Exact match only. Loose prefix matching silently fires the wrong File-menu command
# ("Print..." vs "Print to PDF...", "Export As Version..." vs "Export As PowerPoint...").
function Find-Kid($parent, [string]$name) {
    foreach ($k in $parent.FindAll($TS::Children, $TC)) { if ($k.Current.Name -eq $name) { return $k } }
    return $null
}
function Wait-Popup($item, [int]$ms = 4000) {
    $deadline = (Get-Date).AddMilliseconds($ms)
    while ((Get-Date) -lt $deadline) {
        foreach ($k in $item.FindAll($TS::Children, $TC)) {
            if ($k.Current.ControlType.ProgrammaticName -match 'Menu' -and $k.FindAll($TS::Children, $TC).Count -gt 0) { return $k }
        }
        Start-Sleep -Milliseconds 150
    }
    return $null
}
function Reset-Menus {
    $root = Get-Root
    if (-not $root) { return }
    $mb = Get-MenuBar $root
    if ($mb) { foreach ($k in $mb.FindAll($TS::Children, $TC)) { Collapse-El $k } }
    Start-Sleep -Milliseconds 300
}
function Use-MenuPathOnce([string[]]$Path) {
    $root = Get-Root; if (-not $root) { throw "no root" }
    $mb = Get-MenuBar $root; if (-not $mb) { throw "no menubar" }
    $top = Find-Kid $mb $Path[0]; if (-not $top) { throw "menu '$($Path[0])' not found" }
    [void](Expand-El $top)
    $cur = Wait-Popup $top; if (-not $cur) { throw "no popup under '$($Path[0])'" }
    for ($i = 1; $i -lt $Path.Count; $i++) {
        $item = Find-Kid $cur $Path[$i]; if (-not $item) { throw "item '$($Path[$i])' not found" }
        if ($i -eq $Path.Count - 1) { Invoke-El $item; return $item }
        [void](Expand-El $item)
        $cur = Wait-Popup $item; if (-not $cur) { throw "no popup under '$($Path[$i])'" }
    }
}
function Use-MenuPath([string[]]$Path) {
    $last = $null
    for ($a = 1; $a -le 5; $a++) {
        try { return (Use-MenuPathOnce $Path) } catch { $last = $_; Reset-Menus; Start-Sleep -Milliseconds (300 * $a) }
    }
    throw $last
}
function Get-DialogByTitle([string]$Title, [int]$TimeoutSec = 20) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $w = Get-TabWindows | Where-Object { $_.Title -eq $Title -and $_.Class -notlike '*Icon' } | Select-Object -First 1
        if ($w) { return $w }
        Start-Sleep -Milliseconds 200
    }
    return $null
}
function Find-Desc($root, [string]$name) {
    foreach ($k in $root.FindAll($TS::Descendants, $TC)) { if ($k.Current.Name -eq $name) { return $k } }
    return $null
}

# Only ever touch dialogs we explicitly recognise. Anything else (credential prompts,
# certificate warnings, upgrade nags) must abort rather than be blind-confirmed.
$script:AllowedDialogs = @('Print to PDF', 'Save PDF', 'File Recovery', 'Confirm Save As')
$script:DialogClasses = @('Qt6511QWindow', '#32770')
function Assert-NoUnknownDialog {
    foreach ($w in Get-TabWindows) {
        if ($script:DialogClasses -notcontains $w.Class) { continue }
        if (-not [Nat]::IsWindowVisible($w.H)) { continue }
        if ($w.Class -eq '#32770' -and $w.Title -like '*PDF*') { continue }
        if ($script:AllowedDialogs -notcontains $w.Title) {
            Log "UNKNOWN DIALOG: '$($w.Title)' [$($w.Class)] - aborting rather than confirming blind"
            return $false
        }
    }
    return $true
}

# ============================ output naming ============================
# Every export gets a unique <WorkbookName>_NNN name. Reusing one name makes Windows raise a
# "Confirm Save As" overwrite prompt, which is an extra dialog to race (it cancelled an
# earlier run outright) and an extra thing that could be mistaken for an unknown dialog.
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
$base = [IO.Path]::GetFileNameWithoutExtension($Twbx)
$seq = 1
while (Test-Path (Join-Path $OutDir ("{0}_{1:D3}.pdf" -f $base, $seq))) { $seq++ }
$OutPdf = Join-Path $OutDir ("{0}_{1:D3}.pdf" -f $base, $seq)

# ============================ MAIN ============================
Log "worker start pid=$PID"
Log "output -> $OutPdf"
# Remember what the user was actually using, so we can hand focus back afterwards.
# Menu expansion inherently activates the Qt window; that is unavoidable via UIA.
$script:UserFg = [Nat]::GetForegroundWindow()
Log "user foreground hwnd=$($script:UserFg)"

$tLaunch = Get-Date
$proc = Start-Process -FilePath $TableauExe -ArgumentList "`"$Twbx`"" -PassThru
$script:TabPid = $proc.Id
Log "tableau pid=$($script:TabPid)"

$loaded = $false
$deadline = (Get-Date).AddSeconds(180)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    $h = Get-MainHwnd
    if ($h -ne [IntPtr]::Zero) {
        $mb = Get-MenuBar ($AE::FromHandle($h))
        if ($mb -and (Find-Kid $mb 'File')) { $loaded = $true; break }
    }
}
$loadSec = ((Get-Date) - $tLaunch).TotalSeconds
Log ("workbook loaded={0} in {1:N1}s" -f $loaded, $loadSec)
if (-not $loaded) { Log "FAILED to load"; exit 2 }

Move-OffScreen (Get-MainHwnd)
Log "parked off-screen"

# "File Recovery" appears after any unclean Tableau shutdown; dismissing it discards nothing.
for ($r = 0; $r -lt 4; $r++) {
    $stray = Get-TabWindows | Where-Object { $_.Class -notlike '*Icon' -and $_.Title -eq 'File Recovery' }
    if (-not $stray) { break }
    foreach ($w in $stray) { Log "dismissing 'File Recovery'"; [void][Nat]::PostMessage($w.H, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) }
    Start-Sleep -Seconds 2
}

# ---- File > Print to PDF... ----
$tFocusLost = Get-Date
$tDlg = Get-Date
Use-MenuPath @('File', 'Print to PDF...') | Out-Null
$dw = Get-DialogByTitle 'Print to PDF' 25
if (-not $dw) { Log "NO Print to PDF dialog"; exit 3 }
Log ("print dialog open in {0:N1}s" -f ((Get-Date) - $tDlg).TotalSeconds)
$dlg = $AE::FromHandle($dw.H)

# Unlike TableauFramelessDialog, this is a classic Qt dialog with REAL QRadioButton /
# QCheckBox / QPushButton controls, so UIA patterns actually work here.
$rb = Find-Desc $dlg 'Entire workbook'
if (-not $rb) { Log "NO 'Entire workbook' radio"; exit 4 }
try {
    $rb.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
    Log "selected 'Entire workbook'"
} catch { Invoke-El $rb; Log "selected 'Entire workbook' (Invoke fallback)" }

# This defaults to ON and would launch a PDF viewer in the customer's face. Must be OFF.
$cb = Find-Desc $dlg 'View PDF file after printing'
if ($cb) {
    $tp = $cb.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
    if ("$($tp.Current.ToggleState)" -ne 'Off') { $tp.Toggle(); Start-Sleep -Milliseconds 250 }
    Log "view-after-printing = $($tp.Current.ToggleState)"
}

if (-not (Assert-NoUnknownDialog)) { Log "aborting"; exit 6 }

$ok0 = Find-Desc $dlg 'OK'
if (-not $ok0) { Log "NO OK button"; exit 4 }
Invoke-El $ok0
Log "pressed OK"

# ---- save dialog ----
$sw = $null
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    $sw = Get-TabWindows | Where-Object { $_.Class -eq '#32770' } | Select-Object -First 1
    if ($sw) { break }
    Start-Sleep -Milliseconds 150
}
if (-not $sw) { Log "NO save dialog"; exit 5 }
Log "save dialog '$($sw.Title)' hwnd=$($sw.H)"

# Resolve the filename box and Save button from the real child-window tree. They are exposed
# to UIA only as ControlType.Pane, and they do not exist the instant the dialog HWND appears.
$editH = [IntPtr]::Zero; $saveH = [IntPtr]::Zero
$tRes = Get-Date
$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    $kids = [Nat]::Walk($sw.H, 3)
    $e = $kids | Where-Object { $_.Cls -eq 'Edit' } | Select-Object -First 1
    $s = $kids | Where-Object { $_.Cls -eq 'Button' -and ($_.Txt -replace '&', '') -eq 'Save' } | Select-Object -First 1
    if ($e -and $s) { $editH = $e.H; $saveH = $s.H; break }
    Start-Sleep -Milliseconds 150
}
Log ("resolved edit={0} save={1} in {2:N2}s" -f $editH, $saveH, ((Get-Date) - $tRes).TotalSeconds)
if ($editH -eq [IntPtr]::Zero -or $saveH -eq [IntPtr]::Zero) {
    foreach ($k in [Nat]::Walk($sw.H, 3)) { Log ("  d{0} [{1}] '{2}'" -f $k.Depth, $k.Cls, $k.Txt) }
    Log "FAILED: save controls not found"; exit 5
}

[void][Nat]::SendMessageW($editH, 0x000C, [IntPtr]::Zero, $OutPdf)        # WM_SETTEXT
# WM_SETTEXT changes the edit control's text but NOT the Vista-style IFileDialog's own model,
# so IDOK would still commit the original default filename. Raising EN_CHANGE on the parent is
# what makes the dialog re-read the box.
$ctrlId = [Nat]::GetDlgCtrlID($editH)
$parent = [Nat]::GetParent($editH)
$wp = [IntPtr](([int]0x0300 -shl 16) -bor ($ctrlId -band 0xFFFF))          # EN_CHANGE
[void][Nat]::SendMessage($parent, 0x0111, $wp, $editH)                     # WM_COMMAND
Start-Sleep -Milliseconds 250
$sb = New-Object Text.StringBuilder 1024
[void][Nat]::SendMessageW($editH, 0x000D, [IntPtr]1024, $sb)              # WM_GETTEXT
Log "filename readback: '$($sb.ToString())' (ctrlId=$ctrlId)"
if ($sb.ToString() -ne $OutPdf) { Log "WARN: filename did not take" }

# BM_CLICK to a button in another process is unreliable. WM_COMMAND with BN_CLICKED and the
# button's control id (IDOK=1) is the canonical focus-free way to press a common-dialog button.
# It MUST be posted, not sent: SendMessage blocks until the target thread finishes handling it,
# and the save handler itself runs modally, so a synchronous send deadlocks the worker.
[void][Nat]::PostMessage($sw.H, 0x0111, [IntPtr]1, $saveH)                # WM_COMMAND / IDOK
Log "pressed Save (WM_COMMAND IDOK, posted)"

# Hand the foreground back immediately - everything from here on is focus-free, so the user
# gets their machine back while Tableau is still rendering the PDF.
if ($script:UserFg -ne [IntPtr]::Zero -and [Nat]::IsWindow($script:UserFg)) {
    [void][Nat]::SetForegroundWindow($script:UserFg)
    Log ("restored user foreground after {0:N1}s of disruption" -f ((Get-Date) - $tFocusLost).TotalSeconds)
}

# Unique naming should prevent this. If it does appear, only confirm it when it is talking
# about OUR target file - blindly answering Yes could overwrite one of the customer's files.
$script:OverwriteBlocked = $false
for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Milliseconds 250
    $cf = Get-TabWindows | Where-Object { $_.Title -eq 'Confirm Save As' } | Select-Object -First 1
    if (-not $cf) { continue }
    $kids = [Nat]::Walk($cf.H, 2)
    $msg = ($kids | Where-Object { $_.Cls -like 'Static*' -and $_.Txt } | ForEach-Object { $_.Txt }) -join ' | '
    Log "Confirm Save As says: $msg"
    $mine = [IO.Path]::GetFileName($OutPdf)
    if ($msg -like "*$mine*") {
        $yes = $kids | Where-Object { $_.Cls -eq 'Button' -and ($_.Txt -replace '&', '') -eq 'Yes' } | Select-Object -First 1
        if ($yes) { [void][Nat]::PostMessage($cf.H, 0x0111, [IntPtr]6, $yes.H); Log "confirmed overwrite of our own output" }
    } else {
        $no = $kids | Where-Object { $_.Cls -eq 'Button' -and ($_.Txt -replace '&', '') -eq 'No' } | Select-Object -First 1
        if ($no) { [void][Nat]::PostMessage($cf.H, 0x0111, [IntPtr]7, $no.H) }
        Log "REFUSED overwrite - prompt does not refer to our output file"
        $script:OverwriteBlocked = $true
    }
    break
}

# ---- wait for the file to appear ----
# NOTE: a settled file SIZE does not mean the PDF is finished. Tableau streams each sheet as it
# renders and stalls for seconds between them, so the size sits still at a fraction of the final
# bytes (observed: steady at 70,516 while the finished file was 456,722). Treat this only as
# "the export has started"; completeness is asserted after the idle wait, below.
$appeared = $false
for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Milliseconds 500
    if ((Test-Path $OutPdf) -and (Get-Item $OutPdf).Length -gt 0) { $appeared = $true; break }
}
$totalSec = ((Get-Date) - $script:T0).TotalSeconds
if ($appeared) { Log ("export started: {0} ({1:N0} bytes so far) at {2:N1}s (load {3:N1}s)" -f (Split-Path $OutPdf -Leaf), (Get-Item $OutPdf).Length, $totalSec, $loadSec) }
else { Log "FAILED: no file at $OutPdf"; Log "windows: $((Get-TabWindows | ForEach-Object { "$($_.Title)[$($_.Class)]" }) -join ', ')" }

# Tableau writes the PDF to disk BEFORE the print job is actually finished: it carries on
# rendering the remaining sheets behind progress windows ("Print to PDF", "Processing Request",
# "Computing filters for ... ", "Sorting data"). WM_CLOSE posted during that work is simply
# ignored, the close times out, and force-killing plants a "File Recovery" dialog that breaks
# the NEXT run. So wait for the process to go quiet first. This costs wall-clock time but
# happens entirely after the user's foreground has been handed back, so it is not disruption.
$idleDeadline = (Get-Date).AddSeconds(120)
$quietSince = $null
while ((Get-Date) -lt $idleDeadline) {
    if (-not (Get-Process -Id $script:TabPid -ErrorAction SilentlyContinue)) { break }
    $busy = @(Get-CloseWindows | Where-Object { $_.Class -notlike '*QWindowIcon' })
    if ($busy.Count -gt 0) {
        $quietSince = $null
        $t = $busy[0].Title
        if ($t -ne $script:LastBusy) { $script:LastBusy = $t; Log "still busy: '$t'" }
    }
    else {
        if (-not $quietSince) { $quietSince = Get-Date }
        if (((Get-Date) - $quietSince).TotalMilliseconds -ge 1500) { break }
    }
    Start-Sleep -Milliseconds 250
}
$idleSec = ((Get-Date) - $script:T0).TotalSeconds - $totalSec
Log ("idle after {0:N1}s of post-write work" -f $idleSec)

# Now that the renderer is quiet, assert the PDF is actually COMPLETE rather than merely
# present: a valid PDF ends with the %%EOF trailer, so a truncated export is caught here
# instead of being handed downstream as a silently-missing-pages image set.
$ok = $false
for ($i = 0; $i -lt 40; $i++) {
    if (Test-Path $OutPdf) {
        try {
            $fs = [IO.File]::Open($OutPdf, 'Open', 'Read', 'ReadWrite')
            try {
                $len = $fs.Length
                if ($len -gt 1024) {
                    $tail = New-Object byte[] 1024
                    [void]$fs.Seek(-1024, 'End')
                    [void]$fs.Read($tail, 0, 1024)
                    if (([Text.Encoding]::ASCII.GetString($tail)) -match '%%EOF') { $ok = $true }
                }
            }
            finally { $fs.Close() }
        }
        catch {}
    }
    if ($ok) { break }
    Start-Sleep -Milliseconds 500
}
if ($ok) { Log ("SUCCESS: {0} = {1:N0} bytes (complete, %%EOF present)" -f (Split-Path $OutPdf -Leaf), (Get-Item $OutPdf).Length) }
elseif ($appeared) { Log ("FAILED: {0} is truncated - no %%EOF after idle ({1:N0} bytes)" -f (Split-Path $OutPdf -Leaf), (Get-Item $OutPdf).Length) }

# Force-killing leaves recovery state behind, so always close cleanly. If printing marked the
# workbook dirty, Qt puts up a modal "save your changes?" prompt that blocks the exit; answer
# it with the DISCARD button only - the customer's workbook must never be written to.
$mh = Get-MainHwnd
if ($mh -ne [IntPtr]::Zero) { [void][Nat]::PostMessage($mh, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) }
$gone = $false
$seenClose = @{}
$lastPost = Get-Date
for ($i = 0; $i -lt 240; $i++) {
    Start-Sleep -Milliseconds 250
    if (-not (Get-Process -Id $script:TabPid -ErrorAction SilentlyContinue)) { $gone = $true; break }
    $wins = @(Get-CloseWindows)
    $mains = @($wins | Where-Object { $_.Class -like '*QWindowIcon' })
    $sig = ($mains | ForEach-Object { "$($_.H):$($_.Title)" }) -join ','
    if ($sig -ne $script:LastMainSig) { $script:LastMainSig = $sig; Log "close: main windows -> [$sig]" }
    # Closing the document does not quit Tableau: it tears the workbook window down and swaps
    # in a start-page window - same class, title just "Tableau", but a NEW hwnd - so a single
    # WM_CLOSE to the original handle leaves the process running forever. Re-post to whatever
    # main window is currently present until the process actually exits.
    if (((Get-Date) - $lastPost).TotalMilliseconds -ge 1000) {
        foreach ($w in $mains) { [void][Nat]::PostMessage($w.H, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) }
        $lastPost = Get-Date
    }
    foreach ($w in ($wins | Where-Object { $_.Class -notlike '*QWindowIcon' })) {
        $key = "$($w.H)|$($w.Title)"
        if (-not $seenClose.ContainsKey($key)) {
            $seenClose[$key] = $true
            $kids = @([Nat]::Walk($w.H, 6) | Where-Object { $_.Txt -and $_.Txt.Trim() } | ForEach-Object { "[$($_.Cls)]'$($_.Txt)'" })
            Log "close-blocker: cls='$($w.Class)' title='$($w.Title)' :: $($kids -join ' ')"
        }
        # Answer a save-changes prompt by discarding. Never press Save.
        $btn = Invoke-DiscardButton $w.H
        if ($btn) { Log "close: pressed '$btn'" }
    }
}
if (-not $gone) { Log "graceful close timed out - forcing"; try { Stop-Process -Id $script:TabPid -Force -ErrorAction SilentlyContinue } catch {} }
else { Log ("closed gracefully in {0:N1}s" -f (((Get-Date) - $script:T0).TotalSeconds - $totalSec - $idleSec)) }
Log ("worker done total={0:N1}s capture={1:N1}s" -f ((Get-Date) - $script:T0).TotalSeconds, $totalSec)
if ($ok) { Write-Output $OutPdf; exit 0 } else { exit 1 }
