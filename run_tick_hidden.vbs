' Hidden launcher for the OSS Contributor tick.
' Windows Task Scheduler is unreliable at honouring powershell.exe -WindowStyle Hidden
' (the console window still flashes up every tick), so start it through wscript with
' intWindowStyle = 0, which hides the window completely.
Option Explicit

Dim shell, command, exitCode
Set shell = CreateObject("WScript.Shell")

command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\Users\lyp99\Documents\GitHub\oss-contributor\run_tick.ps1"""

' 0 = hidden window, True = wait for the tick to finish so the scheduled task
' observes the real exit code instead of returning immediately.
exitCode = shell.Run(command, 0, True)

Set shell = Nothing
WScript.Quit exitCode
