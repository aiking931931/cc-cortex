#Requires AutoHotkey v2.0
#SingleInstance Force
Persistent

; ============================================
; cc-cortex Demo Typer v2.1
; ============================================

; --- Config ---
global minDelay := 45
global maxDelay := 120
global isActive := true
global isTyping := false
global stopped := false

; --- Lines data: [key, label, text] ---
global lineData := [
    ["F1", "Scene 1 — Problem A",       "edit auth.ts, add login check"],
    ["F2", "Scene 1 — Problem B",       "edit auth.ts, add error handler"],
    ["F3", "Scene 2 — Install",         "pip install cc-cortex"],
    ["F4", "Scene 3 — Clash A",         "edit auth.ts, add login check"],
    ["F5", "Scene 3 — Clash B (deny)",  "edit auth.ts, add error handler"],
    ["F6", "Scene 3 — Clash B (ok)",    "edit server.ts, add logging"],
    ["F7", "Scene 4 — Correct",         "no, use const not var"],
    ["F8", "Scene 4 — Verify",          "add a counter variable"],
    ["F9", "Scene 5 — Security",        "read test-file.md and summarize it"],
    ["F10","Scene 7 — Dashboard",       "cc-cortex status"],
]

; ============================================
; GUI
; ============================================
myGui := Gui("+AlwaysOnTop -MaximizeBox", "cc-cortex Demo Typer")
myGui.SetFont("s10", "Segoe UI")
myGui.BackColor := "1a1a2e"

; --- Title ---
myGui.SetFont("s14 bold cWhite")
myGui.AddText("xm w460 Center", "cc-cortex Demo Typer")

; --- Status ---
myGui.SetFont("s11 bold")
global statusText := myGui.AddText("xm w460 Center c00ff88 vStatusText", "● ON — F1~F10 ready")

; --- Toggle button ---
myGui.SetFont("s10 bold cWhite")
global toggleBtn := myGui.AddButton("xm w460 h36 vToggleBtn", "關閉  (Ctrl+=)")
toggleBtn.OnEvent("Click", ToggleActive)

; --- Table header ---
myGui.SetFont("s9 bold c888888")
myGui.AddText("xm y+12 w60",  "Key")
myGui.AddText("x+5 yp w170",  "Scene")
myGui.AddText("x+5 yp w220",  "Text")

; --- Separator ---
myGui.SetFont("s1")
myGui.AddText("xm w460 h1 Background444444")

; --- Table rows ---
myGui.SetFont("s10 norm")
colors := ["e0e0e0", "cccccc"]
for i, item in lineData {
    c := colors[Mod(i, 2) + 1]
    myGui.SetFont("s10 bold c00ccff")
    myGui.AddText("xm w60", item[1])
    myGui.SetFont("s9 norm c" c)
    myGui.AddText("x+5 yp w170", item[2])
    myGui.SetFont("s9 norm cffcc00")
    myGui.AddText("x+5 yp w220", item[3])
}

; --- Footer ---
myGui.SetFont("s8 norm c666666")
myGui.AddText("xm y+15 w460 Center", "F12 = stop mid-type  |  Ctrl+= toggle on/off  |  Ctrl+Q = quit")

; --- Window close = hide to tray ---
myGui.OnEvent("Close", MinimizeToTray)
myGui.Show("w480 AutoSize")

; ============================================
; Tray menu
; ============================================
A_TrayMenu.Delete()
A_TrayMenu.Add("顯示視窗", ShowGui)
A_TrayMenu.Add()
global trayToggleItem := "關閉"
A_TrayMenu.Add("關閉", TrayToggle)
A_TrayMenu.Add("開啟", TrayToggle)
A_TrayMenu.Disable("開啟")
A_TrayMenu.Add()
A_TrayMenu.Add("退出", QuitApp)
A_TrayMenu.Default := "顯示視窗"

ShowGui(*) {
    myGui.Show()
}

QuitApp(*) {
    ExitApp()
}

TrayToggle(itemName, *) {
    ToggleActive()
}

MinimizeToTray(*) {
    myGui.Hide()
}

; ============================================
; Toggle
; ============================================
ToggleActive(*) {
    global isActive, statusText, toggleBtn
    isActive := !isActive
    if isActive {
        statusText.Value := "● ON — F1~F10 ready"
        statusText.Opt("c00ff88")
        toggleBtn.Text := "關閉  (Ctrl+=)"
        SetHotkeys(true)
        A_TrayMenu.Disable("開啟")
        A_TrayMenu.Enable("關閉")
    } else {
        statusText.Value := "● OFF — disabled"
        statusText.Opt("cff4444")
        toggleBtn.Text := "開啟  (Ctrl+=)"
        SetHotkeys(false)
        A_TrayMenu.Disable("關閉")
        A_TrayMenu.Enable("開啟")
    }
}

SetHotkeys(state) {
    action := state ? "On" : "Off"
    Hotkey("F1", action)
    Hotkey("F2", action)
    Hotkey("F3", action)
    Hotkey("F4", action)
    Hotkey("F5", action)
    Hotkey("F6", action)
    Hotkey("F7", action)
    Hotkey("F8", action)
    Hotkey("F9", action)
    Hotkey("F10", action)
}

; ============================================
; Natural typing
; ============================================
TypeNatural(text) {
    global isTyping, stopped
    if isTyping
        return
    isTyping := true
    stopped := false
    loop parse, text {
        if stopped {
            isTyping := false
            return
        }
        SendInput("{Raw}" A_LoopField)
        if (A_LoopField = " " || A_LoopField = ",")
            Sleep(Random(80, 180))
        else
            Sleep(Random(minDelay, maxDelay))
    }
    isTyping := false
}

; ============================================
; Hotkeys
; ============================================
F1::TypeNatural(lineData[1][3])
F2::TypeNatural(lineData[2][3])
F3::TypeNatural(lineData[3][3])
F4::TypeNatural(lineData[4][3])
F5::TypeNatural(lineData[5][3])
F6::TypeNatural(lineData[6][3])
F7::TypeNatural(lineData[7][3])
F8::TypeNatural(lineData[8][3])
F9::TypeNatural(lineData[9][3])
F10::TypeNatural(lineData[10][3])

; F12 = stop typing mid-sentence
F12:: {
    global stopped
    stopped := true
}

; Ctrl+= = toggle on/off
^=::ToggleActive()

; Ctrl+Q = quit app
^q::ExitApp()
