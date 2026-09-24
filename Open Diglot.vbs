' Open the diglot reader without a console window.
'
' Double-click this file. It starts the app with pythonw.exe, which has no console
' at all, so nothing flashes up and nothing stays open afterwards; the server's log
' goes to data\app.log instead. Then it opens the reader in your browser.
'
' If the app is already running, it just opens the browser -- a second copy could
' not have the port, and all you wanted was the page.
'
' Run it with a "no-open" argument to start the server without opening a browser:
'     wscript "Open Diglot.vbs" no-open

Option Explicit

Dim shell, fso, root, interpreter, logFile, url, wantsBrowser
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
url = "http://127.0.0.1:8787"
logFile = root & "\data\app.log"
wantsBrowser = True
If WScript.Arguments.Count > 0 Then
    If LCase(WScript.Arguments(0)) = "no-open" Then wantsBrowser = False
End If

If AlreadyRunning(url) Then
    If wantsBrowser Then shell.Run url, 1, False
    WScript.Quit
End If

interpreter = FindInterpreter(root)
If interpreter = "" Then
    MsgBox "Python was not found, so the diglot reader cannot be started." & vbCrLf & vbCrLf & _
           "Install Python, or run this from a terminal to see the error:" & vbCrLf & _
           "    python run.py", 16, "Diglot"
    WScript.Quit
End If

If Not fso.FolderExists(root & "\data") Then fso.CreateFolder(root & "\data")

' 0 = hidden window, and do not wait: the app runs until you close the machine or
' stop it, and this script has nothing left to do once it is started.
shell.CurrentDirectory = root
shell.Run """" & interpreter & """ run.py --log """ & logFile & """", 0, False

' Give it a moment to bind the port before pointing a browser at it, and say so in
' the log if it never does -- the reader's only clue otherwise is a blank tab.
Dim waited, ready
waited = 0
ready = False
Do While waited < 15000
    WScript.Sleep 500
    waited = waited + 500
    If AlreadyRunning(url) Then
        ready = True
        Exit Do
    End If
Loop

If Not ready Then
    If wantsBrowser Then
        MsgBox "The reader did not start within 15 seconds." & vbCrLf & vbCrLf & _
               "What went wrong is written to:" & vbCrLf & logFile, 48, "Diglot"
    End If
    WScript.Quit
End If

If wantsBrowser Then shell.Run url, 1, False

' Is the app answering on its port? Any failure to connect means no.
Function AlreadyRunning(target)
    Dim http
    AlreadyRunning = False
    On Error Resume Next
    Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    http.setTimeouts 400, 400, 700, 700
    http.open "GET", target & "/api/status", False
    http.send
    If Err.Number = 0 Then
        If http.status = 200 Then AlreadyRunning = True
    End If
    On Error GoTo 0
End Function

' The interpreter that has this app's dependencies: a virtual environment beside
' the project first, then whatever pythonw the PATH resolves to -- which is the same
' one `python run.py` would use, so a machine that can run it in a terminal can run
' it here.
Function FindInterpreter(where)
    Dim candidates, i, found
    candidates = Array(where & "\.venv\Scripts\pythonw.exe", _
                       where & "\venv\Scripts\pythonw.exe")
    For i = 0 To UBound(candidates)
        If fso.FileExists(candidates(i)) Then
            FindInterpreter = candidates(i)
            Exit Function
        End If
    Next
    found = WhichOnPath("pythonw.exe")
    If found = "" Then found = WhichOnPath("pyw.exe")
    FindInterpreter = found
End Function

Function WhichOnPath(exe)
    Dim exec, line
    WhichOnPath = ""
    On Error Resume Next
    Set exec = shell.Exec("cmd /c where " & exe)
    If Err.Number <> 0 Then Exit Function
    On Error GoTo 0
    Do While Not exec.StdOut.AtEndOfStream
        line = Trim(exec.StdOut.ReadLine())
        If Len(line) > 0 Then
            WhichOnPath = line
            Exit Function
        End If
    Loop
End Function
