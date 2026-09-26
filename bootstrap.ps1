# ============================================================
#  Vivo Smart Cobertura Worker - Bootstrap
#  Roda a cada logon (chamado por startup_worker.vbs, oculto).
#  Idempotente: verifica ANTES de instalar qualquer coisa.
#
#    1. winget / Git / Python 3.12 / Google Chrome
#    2. git pull  (se houver remote)
#    3. .venv + pip install -r requirements.txt  (so se requirements mudou)
#    4. modelo Whisper 'medium'  (so se faltar)
#    5. inicia worker.py num loop de reinicio
#
#  Uso manual:  powershell -ExecutionPolicy Bypass -File bootstrap.ps1
#               -SkipWorker   -> so provisiona, nao inicia o worker
#               -Once         -> roda o worker uma vez, sem loop
# ============================================================
param(
    [switch]$SkipWorker,
    [switch]$Once
)

$ErrorActionPreference = "Continue"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

# ---------- logging ----------
$LogFile = Join-Path $ProjectDir "bootstrap.log"
if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt 5MB)) {
    Move-Item $LogFile "$LogFile.old" -Force -ErrorAction SilentlyContinue
}
function Log($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $LogFile -Value $line -Encoding utf8
    Write-Host $line
}

function Refresh-Path {
    $m = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $u = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (@($m, $u) | Where-Object { $_ }) -join ";"
}

function Have($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Ensure-Package($id, $friendly, $probe, [string[]]$extraArgs) {
    if (& $probe) { Log "$friendly : ok"; return $true }
    if (-not (Have "winget")) {
        Log "AVISO: '$friendly' ausente e winget indisponivel. Instale manualmente."
        return $false
    }
    Log "Instalando $friendly ($id) via winget..."
    $wgArgs = @("install", "--id", $id, "-e", "--silent",
               "--accept-package-agreements", "--accept-source-agreements")
    if ($extraArgs) { $wgArgs += $extraArgs }
    & winget @wgArgs 2>&1 | ForEach-Object { Log "  winget: $_" }
    Refresh-Path
    if (& $probe) { Log "$friendly instalado."; return $true }
    Log "AVISO: '$friendly' ainda ausente apos winget (pode exigir elevacao no setup_startup.ps1)."
    return $false
}

Log "==================== bootstrap iniciado ===================="
Log "Projeto: $ProjectDir"
if (-not (Have "winget")) { Log "AVISO: winget nao encontrado (App Installer). Instalacoes automaticas ficam limitadas." }

# ---------- 1. Git ----------
Ensure-Package "Git.Git" "Git" { Have "git" } | Out-Null
if (Have "git") { Log ("git " + (& git --version)) }

# ---------- 2. Google Chrome ----------
$chromeProbe = {
    $p = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )
    [bool]($p | Where-Object { Test-Path $_ })
}
Ensure-Package "Google.Chrome" "Google Chrome" $chromeProbe | Out-Null

# ---------- 3. Python 3.12 ----------
function Get-Python312 {
    try {
        $out = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return "$out".Trim() }
    } catch { }
    foreach ($p in @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:ProgramFiles\Python312\python.exe"
    )) { if (Test-Path $p) { return $p } }
    return $null
}
Ensure-Package "Python.Python.3.12" "Python 3.12" { [bool](Get-Python312) } @("--scope", "user") | Out-Null
$Py = Get-Python312
if (-not $Py) { Log "ERRO FATAL: Python 3.12 indisponivel. Abortando."; exit 1 }
Log "Python 3.12: $Py"

# ---------- 4. git pull ----------
if (Have "git") {
    $remote = & git remote 2>$null
    if ($remote) {
        $branch = (& git rev-parse --abbrev-ref HEAD 2>$null)
        if (-not $branch -or $branch -eq "HEAD") { $branch = "main" }
        Log "git pull --ff-only origin $branch"
        & git pull --ff-only origin $branch 2>&1 | ForEach-Object { Log "  git: $_" }
    } else {
        Log "Sem remote configurado; git pull ignorado."
    }
}

# ---------- 5. venv ----------
$VenvDir = Join-Path $ProjectDir ".venv"
$VenvPy  = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Log "Criando venv (.venv)..."
    & $Py -m venv $VenvDir 2>&1 | ForEach-Object { Log "  venv: $_" }
}
if (-not (Test-Path $VenvPy)) { Log "ERRO FATAL: venv nao criado."; exit 1 }

# ---------- 6. dependencias (so quando requirements.txt muda) ----------
$ReqFile  = Join-Path $ProjectDir "requirements.txt"
$HashFile = Join-Path $VenvDir ".requirements.sha256"
$ReqHash  = (Get-FileHash $ReqFile -Algorithm SHA256).Hash
$NeedInstall = $true
if (Test-Path $HashFile) {
    if (((Get-Content $HashFile -Raw).Trim()) -eq $ReqHash) { $NeedInstall = $false }
}
if ($NeedInstall) {
    Log "pip install -r requirements.txt (pode demorar bastante na 1a vez)..."
    & $VenvPy -m pip install --upgrade pip setuptools wheel --quiet 2>&1 | ForEach-Object { Log "  pip: $_" }
    & $VenvPy -m pip install -r $ReqFile 2>&1 | ForEach-Object { Log "  pip: $_" }
    if ($LASTEXITCODE -eq 0) {
        Set-Content -Path $HashFile -Value $ReqHash -Encoding ascii
        Log "Dependencias instaladas."
    } else {
        Log "AVISO: pip install terminou com codigo $LASTEXITCODE. O worker pode nao subir."
    }
} else {
    Log "Dependencias ja atualizadas (requirements.txt inalterado)."
}

# ---------- 7. modelo Whisper 'medium' ----------
$WhisperModel = Join-Path $env:USERPROFILE ".cache\whisper\medium.pt"
if (-not (Test-Path $WhisperModel)) {
    Log "Baixando modelo Whisper 'medium' (~1.5 GB, so na 1a vez)..."
    & $VenvPy -c "import whisper; whisper.load_model('medium')" 2>&1 | ForEach-Object { Log "  whisper: $_" }
    if (Test-Path $WhisperModel) { Log "Modelo Whisper baixado." }
} else {
    Log "Modelo Whisper 'medium': ok"
}

# ---------- 8. worker ----------
if ($SkipWorker) { Log "-SkipWorker: provisionamento concluido, worker nao iniciado."; exit 0 }

$WorkerScript = Join-Path $ProjectDir "worker.py"
$CrashLog = Join-Path $ProjectDir "worker_crash.log"
$fails = 0
while ($true) {
    Log "Iniciando worker.py"
    $start = Get-Date
    # Redireciona stdout/stderr: se o worker morrer ANTES de configurar seu
    # proprio log (import quebrado, excecao sem try/except), sem isso a causa
    # some por completo - nem bootstrap.log nem logs/worker_*.log mostram nada.
    $proc = Start-Process -FilePath $VenvPy `
        -ArgumentList ('"{0}"' -f $WorkerScript) `
        -WorkingDirectory $ProjectDir -WindowStyle Hidden -Wait -PassThru `
        -RedirectStandardOutput $CrashLog -RedirectStandardError "$CrashLog.err"
    $ranFor = [int](New-TimeSpan -Start $start -End (Get-Date)).TotalSeconds
    Log "worker encerrou (exit $($proc.ExitCode)) apos ${ranFor}s."
    if ($proc.ExitCode -ne 0 -or $ranFor -lt 60) {
        foreach ($f in @($CrashLog, "$CrashLog.err")) {
            if ((Test-Path $f) -and (Get-Item $f).Length -gt 0) {
                Log "----- $f -----"
                Get-Content $f -Tail 40 | ForEach-Object { Log "  $_" }
            }
        }
    }

    if ($Once) { break }

    if ($ranFor -lt 60) { $fails++ } else { $fails = 0 }
    if ($fails -ge 5) {
        Log "5 quedas rapidas seguidas. Pausa de 5 min antes de tentar de novo."
        Start-Sleep -Seconds 300
        $fails = 0
    } else {
        Start-Sleep -Seconds 15
    }
}
