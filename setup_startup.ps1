# ============================================================
#  Vivo Smart Worker - Instalador da Tarefa de Startup
#
#  Execute UMA VEZ (clique direito > "Executar com o PowerShell",
#  ou:  powershell -ExecutionPolicy Bypass -File setup_startup.ps1).
#
#  O que ele faz:
#    1. Se eleva a Administrador (necessario p/ instalar Git/Chrome).
#    2. Roda o bootstrap.ps1 -SkipWorker AGORA: instala winget deps,
#       Python 3.12, cria a .venv, instala requirements e baixa o
#       modelo Whisper. (Idempotente - pode rodar de novo sem medo.)
#    3. Registra a Tarefa Agendada "VivoSmartWorker" que, a cada
#       logon, executa startup_worker.vbs (oculto) -> bootstrap.ps1
#       -> git pull -> atualiza deps se mudou -> inicia o worker.
# ============================================================

$ErrorActionPreference = "Stop"
$TaskName   = "VivoSmartWorker"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VbsPath    = Join-Path $ProjectDir "startup_worker.vbs"
$Bootstrap  = Join-Path $ProjectDir "bootstrap.ps1"

# ---------- auto-elevacao ----------
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Solicitando privilegios de Administrador..." -ForegroundColor Yellow
    Start-Process powershell.exe -Verb RunAs -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`""
    )
    exit
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Vivo Smart Worker - Setup de Startup" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "[1/4] Projeto:  $ProjectDir"
Write-Host "[2/4] Launcher: $VbsPath"

foreach ($f in @($VbsPath, $Bootstrap)) {
    if (-not (Test-Path $f)) {
        Write-Host "ERRO: arquivo nao encontrado: $f" -ForegroundColor Red
        exit 1
    }
}

# ---------- provisionamento agora (com elevacao) ----------
Write-Host ""
Write-Host "[3/4] Provisionando ambiente (bootstrap.ps1 -SkipWorker)..." -ForegroundColor Yellow
Write-Host "      Pode demorar bastante na 1a vez (Python, torch, Whisper ~1.5GB)."
Write-Host ""
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Bootstrap -SkipWorker
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "AVISO: bootstrap terminou com codigo $LASTEXITCODE. Confira bootstrap.log." -ForegroundColor Yellow
}

# ---------- registra a tarefa ----------
Write-Host ""
Write-Host "[4/4] Registrando a Tarefa Agendada '$TaskName'..." -ForegroundColor Yellow
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$VbsPath`"" -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -RunLevel Limited `
    -Description "Worker Vivo Smart APIs. No logon: git pull, atualiza deps se mudou, inicia o robo. Executa oculto." `
    -Force | Out-Null

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  SUCESSO! Tarefa '$TaskName' registrada." -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "A cada logon deste usuario:" -ForegroundColor White
Write-Host "  1. git pull origin <branch>        (se houver remote)" -ForegroundColor Gray
Write-Host "  2. pip install -r requirements.txt  (so se o arquivo mudou)" -ForegroundColor Gray
Write-Host "  3. baixa o modelo Whisper           (so se faltar)" -ForegroundColor Gray
Write-Host "  4. python worker.py                 (loop com auto-reinicio)" -ForegroundColor Gray
Write-Host ""
Write-Host "Tudo oculto. Logs em:  bootstrap.log  e  logs\worker_*.log" -ForegroundColor Gray
Write-Host ""
Write-Host "Testar agora sem reiniciar:  Start-ScheduledTask -TaskName $TaskName" -ForegroundColor Gray
Write-Host "Parar:                       Stop-ScheduledTask  -TaskName $TaskName" -ForegroundColor Gray
Write-Host ""
if (-not (& git -C $ProjectDir remote 2>$null)) {
    Write-Host "NOTA: nenhum remote git configurado - o 'git pull' e pulado." -ForegroundColor Yellow
    Write-Host "      Depois:  git remote add origin <URL> ; git push -u origin main" -ForegroundColor Yellow
    Write-Host ""
}
