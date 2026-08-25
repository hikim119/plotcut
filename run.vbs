' PlotCut launcher - the Desktop shortcut points here.
'
' It used to run `pythonw gui.py` hidden (0) right away. If startup failed,
' **nothing at all happened** - no window, no console, no error. For a
' first-time user that is the worst possible outcome. (`run.bat` does check,
' but the shortcut does not use run.bat.)
'
' So we probe once, quietly, and say what to do when it fails.
'
' **ASCII only.** WSH reads .vbs in the system codepage, so UTF-8 Korean in
' this file comes out as mojibake in the dialog (verified with cscript).
' Every other launcher here is ASCII for the same reason - keep it that way.

Dim fso, dir, script, sh, rc
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
script = dir & "\gui.py"
Set sh = CreateObject("WScript.Shell")

If Not fso.FileExists(script) Then
    MsgBox "gui.py not found:" & vbCrLf & script & vbCrLf & vbCrLf & _
           "If you moved the PlotCut folder, run setup.bat again.", _
           vbExclamation, "PlotCut"
    WScript.Quit 1
End If

sh.CurrentDirectory = dir

' Python present and the modules importable? Non-zero means it cannot start.
On Error Resume Next
rc = sh.Run("python -c ""import tkinter, guards, pipeline, script_io""", 0, True)
If Err.Number <> 0 Then
    MsgBox "Python not found." & vbCrLf & vbCrLf & _
           "Run setup.bat in this folder first.", vbExclamation, "PlotCut"
    WScript.Quit 1
End If
On Error GoTo 0

If rc <> 0 Then
    MsgBox "PlotCut cannot start." & vbCrLf & vbCrLf & _
           "Run setup.bat in this folder first." & vbCrLf & _
           "If it still fails, run run.bat to see the actual error.", _
           vbExclamation, "PlotCut"
    WScript.Quit 1
End If

sh.Run "pythonw """ & script & """", 0, False
