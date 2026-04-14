#Requires AutoHotkey v2.0
#SingleInstance Force
Persistent

; ============================================
; Auto Typer — 自訂文字自然打字器
; ============================================

global minDelay := 45
global maxDelay := 120
global isActive := true
global isTyping := false
global stopped := false
global slots := Map()
global slotEdits := Map()

; --- Load saved slots from ini ---
iniPath := A_ScriptDir "\auto-typer-slots.ini"
Loop 10 {
    slots[A_Index] := IniRead(iniPath, "Slots", "F" A_Index, "")
}

; ============================================
; GUI
; ============================================
myGui := Gui("+AlwaysOnTop -MaximizeBox +Resize", "Auto Typer")
myGui.SetFont("s10", "Segoe UI")
myGui.BackColor := "1a1a2e"

; --- Title ---
myGui.SetFont("s14 bold cWhite")
myGui.AddText("xm w540 Center", "Auto Typer")

; --- Status ---
myGui.SetFont("s11 bold")
global statusText := myGui.AddText("xm w540 Center c00ff88", "● ON — F1~F10 ready")

; --- Toggle ---
myGui.SetFont("s10 bold cWhite")
global toggleBtn := myGui.AddButton("xm w540 h32", "關閉  (Ctrl+=)")
toggleBtn.OnEvent("Click", ToggleActive)

; --- Slot rows ---
myGui.SetFont("s9 bold c888888")
myGui.AddText("xm y+10 w45", "Key")
myGui.AddText("x+5 yp w490", "Text (type here, auto-saved)")
myGui.SetFont("s1")
myGui.AddText("xm w540 h1 Background444444")

myGui.SetFont("s10 norm")
Loop 10 {
    n := A_Index
    myGui.SetFont("s10 bold c00ccff")
    myGui.AddText("xm w45 h26 +0x200", "F" n)
    myGui.SetFont("s10 norm cWhite")
    ed := myGui.AddEdit("x+5 yp w490 h26 Background2a2a4e cWhite", slots[n])
    ed.OnEvent("Change", SaveSlot.Bind(n))
    slotEdits[n] := ed
}

; --- Quick paste area ---
myGui.SetFont("s9 bold c888888")
myGui.AddText("xm y+12 w540", "Quick Type — paste anything, press Ctrl+Enter to type it")
myGui.SetFont("s10 norm cWhite")
global quickEdit := myGui.AddEdit("xm w540 h60 Background2a2a4e cWhite Multi")

myGui.SetFont("s10 bold cWhite")
quickBtn := myGui.AddButton("xm w540 h32", "Type it  (Ctrl+Enter)")
quickBtn.OnEvent("Click", TypeQuickText)

; --- Footer ---
myGui.SetFont("s8 norm c666666")
myGui.AddText("xm y+10 w540 Center", "F12 = stop mid-type  |  Ctrl+= on/off  |  Ctrl+Enter = quick type  |  Ctrl+Q = quit")

myGui.OnEvent("Close", MinimizeToTray)
myGui.Show("w580 AutoSize")

; ============================================
; Save slot on edit
; ============================================
SaveSlot(n, ctrl, *) {
    global slots
    slots[n] := ctrl.Value
    IniWrite(ctrl.Value, iniPath, "Slots", "F" n)
}

; ============================================
; Tray
; ============================================
A_TrayMenu.Delete()
A_TrayMenu.Add("顯示視窗", ShowGui)
A_TrayMenu.Add()
A_TrayMenu.Add("關閉", TrayToggle)
A_TrayMenu.Add("開啟", TrayToggle)
A_TrayMenu.Disable("開啟")
A_TrayMenu.Add()
A_TrayMenu.Add("退出", QuitApp)
A_TrayMenu.Default := "顯示視窗"

ShowGui(*) => myGui.Show()
QuitApp(*) => ExitApp()
TrayToggle(*) => ToggleActive()
MinimizeToTray(*) => myGui.Hide()

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
    Loop 10
        Hotkey("F" A_Index, action)
}

; ============================================
; Natural typing
; ============================================
TypeNatural(text) {
    global isTyping, stopped
    if isTyping || text = ""
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

TypeSlot(n, *) {
    global slots
    TypeNatural(slots[n])
}

TypeQuickText(*) {
    global quickEdit
    TypeNatural(quickEdit.Value)
}

; ============================================
; Hotkeys
; ============================================
F1::TypeSlot(1)
F2::TypeSlot(2)
F3::TypeSlot(3)
F4::TypeSlot(4)
F5::TypeSlot(5)
F6::TypeSlot(6)
F7::TypeSlot(7)
F8::TypeSlot(8)
F9::TypeSlot(9)
F10::TypeSlot(10)

F12:: {
    global stopped
    stopped := true
}

^=::ToggleActive()
^Enter::TypeQuickText()
^q::ExitApp()
