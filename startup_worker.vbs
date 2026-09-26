' ============================================================
'  Vivo Smart Worker - Launcher oculto
'  Chamado pela Tarefa Agendada no logon. Executa o
'  bootstrap.ps1 em segundo plano, SEM nenhuma janela.
' ============================================================
Option Explicit
Dim shell, fso, scriptDir, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & _
      scriptDir & "\bootstrap.ps1"""

' 0 = janela oculta ; False = nao espera terminar
shell.Run cmd, 0, False
