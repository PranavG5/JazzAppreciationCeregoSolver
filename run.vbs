Set objShell = CreateObject("WScript.Shell")
objShell.Run "pythonw """ & CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\solver.py""", 0, False
