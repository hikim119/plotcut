Dim fso, dir, script
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
script = dir & "\gui.py"
CreateObject("WScript.Shell").Run "pythonw """ & script & """", 0, False
