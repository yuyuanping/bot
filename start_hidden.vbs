' Window-free entry point: runs start.bat with wShowWindow = 0 (SW_HIDE).
' Double-click this instead of start.bat to avoid the launcher console.
CreateObject("Wscript.Shell").Run """D:\opencword\bot\start.bat""", 0, False
