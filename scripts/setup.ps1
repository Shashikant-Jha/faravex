<#
.SYNOPSIS
  Checks the faravex environment, installs only what is missing or broken, then starts the browser UI.
  No admin rights or WSL needed: everything goes into this folder (.venv, .tools) or your user profile.

.EXAMPLE
  start.bat                      check, repair if needed, then open the UI
  start.bat -Mode check          only report what is installed / missing (installs nothing)
  start.bat -Mode setup          check and repair, do not start
  start.bat -Python 3.12 -Force  rebuild .venv on another Python version (dependency conflicts)
  start.bat -Locked              install the exact versions from requirements-lock.txt
  start.bat -Torch cpu           force the CPU build of torch
#>
param(
  [ValidateSet('ui', 'setup', 'check')] [string]$Mode = 'ui',
  [string]$Python = '3.13',
  [ValidateSet('auto', 'cu130', 'cu128', 'cu126', 'cpu')] [string]$Torch = 'auto',
  [switch]$Locked,
  [switch]$Force,
  [switch]$NoBrowser,
  [int]$Port = 0
)

# Native tools (py, uv, pip) write progress to stderr; with 'Stop', PowerShell 5.1 turns redirected stderr into
# terminating errors. Exit codes are checked explicitly instead.
$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root '.venv'
$Py = Join-Path $Venv 'Scripts\python.exe'
$SitePackages = Join-Path $Venv 'Lib\site-packages'
$Tools = Join-Path $Root '.tools'
$CheckDeps = Join-Path $PSScriptRoot 'check_deps.py'
$Req = Join-Path $Root $(if ($Locked) { 'requirements-lock.txt' } else { 'requirements.txt' })
$ModelDir = if ($env:FARAVEX_MODEL_PATH) { $env:FARAVEX_MODEL_PATH } else { Join-Path $Root 'models\embeddinggemma' }
$HasModel = Test-Path (Join-Path $ModelDir 'config.json')

function Say([string]$msg) { Write-Host "[faravex] $msg" }
function Warn([string]$msg) { Write-Host "[faravex] $msg" -ForegroundColor Yellow }
function Fail([string]$msg) { Write-Host "[faravex] $msg" -ForegroundColor Red; exit 1 }

# ---------- GPU / torch build ----------
function Get-TorchVariant {
  if ($Torch -ne 'auto') { return $Torch }
  $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
  if (-not $smi) { Say 'No NVIDIA driver found: using the CPU build of torch.'; return 'cpu' }
  $line = (& nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>$null | Select-Object -First 1)
  if (-not $line) { return 'cpu' }
  $parts = $line.Split(',') | ForEach-Object { $_.Trim() }
  Say "GPU: $($parts[0]), driver $($parts[1]), $($parts[2])"
  $major = [int]($parts[1].Split('.')[0])
  # CUDA 13.0 wheels need driver R580+. CUDA 12.x wheels run on any R525+ driver (minor-version compatibility).
  if ($major -ge 580) { return 'cu130' }
  if ($major -ge 525) { return 'cu126' }
  Warn "Driver $($parts[1]) is older than R525: using CPU torch. Update the driver for GPU speed."
  return 'cpu'
}

# What check_deps.py accepts as a good torch: auto mode takes any CUDA build that works (an installed cu128 is
# fine on a cu130 driver, no re-download); an explicit -Torch cuXXX wants exactly that build.
function Get-TorchWant([string]$variant) {
  if ($variant -eq 'cpu') { return 'any' }
  if ($Torch -eq 'auto') { return 'cuda' }
  return $variant
}

# ---------- uv (single exe, kept in .tools) ----------
function Get-Uv {
  $cmd = Get-Command uv -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $local = Join-Path $Tools 'uv.exe'
  if (Test-Path $local) { return $local }
  Say 'Downloading uv (Python and package manager, ~20 MB) into .tools ...'
  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    New-Item -ItemType Directory -Force $Tools | Out-Null
    $zip = Join-Path $Tools 'uv.zip'
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -ErrorAction Stop -UseBasicParsing 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile $zip
    Expand-Archive -ErrorAction Stop -Force $zip $Tools
    Remove-Item $zip
    if (Test-Path $local) { return $local }
  } catch {
    Warn "Could not download uv ($($_.Exception.Message)). Falling back to an installed Python."
  }
  return $null
}

function Find-SystemPython {
  $py = Get-Command py -ErrorAction SilentlyContinue
  foreach ($v in @($Python, '3.13', '3.12', '3.11', '3.10') | Select-Object -Unique) {
    if ($py) {
      & py "-$v" -c "import sys" 2>$null
      if ($LASTEXITCODE -eq 0) { return @('py', "-$v") }
    }
  }
  $python = Get-Command python -ErrorAction SilentlyContinue
  if ($python -and $python.Source -notlike '*WindowsApps*') {
    $ok = & python -c "import sys; print(int(sys.version_info >= (3, 10)))" 2>$null
    if ($ok -eq '1') { return @('python') }
  }
  return $null
}

# ---------- venv ----------
function Get-VenvVersion {
  if (-not (Test-Path $Py)) { return $null }
  $v = & $Py -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
  if ($LASTEXITCODE -ne 0) { return $null }
  return $v
}

# A venv made with --system-site-packages also sees the system Python's packages (e.g. a CUDA torch installed there).
# pip counts those as installed; uv does not and would download a second copy into .venv that hides the system one.
function Test-InheritsSystem {
  $cfg = Join-Path $Venv 'pyvenv.cfg'
  if (-not (Test-Path $cfg)) { return $false }
  return [bool](Select-String -Path $cfg -Pattern '^\s*include-system-site-packages\s*=\s*true' -Quiet)
}

function New-Venv($uv) {
  if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
  if ($uv) {
    Say "Creating .venv with Python $Python (uv downloads it into your user profile if it is missing) ..."
    & $uv venv $Venv --python $Python --seed | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail "uv could not create a Python $Python environment." }
    return
  }
  $sys = Find-SystemPython
  if (-not $sys) {
    Fail ('No Python 3.10+ found and uv could not be downloaded. Install Python for your user only ' +
      '(python.org installer, untick "for all users"; no admin needed) or allow github.com, then run start.bat again.')
  }
  Say "Creating .venv with $($sys -join ' ') ..."
  $exe = $sys[0]; $rest = @($sys | Select-Object -Skip 1)
  & $exe @rest -m venv $Venv | Out-Host
  if ($LASTEXITCODE -ne 0) { Fail 'python -m venv failed.' }
  & $Py -m pip install --quiet --upgrade pip | Out-Host
}

# ---------- checks ----------
# Runs check_deps.py; returns its bit mask (1 = requirements, 2 = torch). Problems are printed as warnings.
function Invoke-Check([string]$want, [switch]$Show) {
  $cargs = @($CheckDeps, $Req, '--torch', $want)
  if ($Show) { $cargs += '-v' }
  $out = & $Py @cargs
  $code = $LASTEXITCODE
  foreach ($l in $out) { if ($Show -and $code -eq 0) { Say $l } else { Warn $l } }
  return $code
}

# ---------- install ----------
# Native output goes to Out-Host: anything left in a function's output stream becomes part of its return value.
function Install-Packages([string[]]$pkgArgs) {
  if ($script:UsePip) { & $Py -m pip install --disable-pip-version-check @pkgArgs | Out-Host } else { & $script:Uv pip install --python $Py @pkgArgs | Out-Host }
  return ($LASTEXITCODE -eq 0)
}

# Uninstalls torch / torchvision only where they sit inside .venv; a system copy is never touched.
function Remove-VenvTorch {
  # @() keeps a single match an array; a bare string would be splatted character by character.
  $names = @(@('torch', 'torchvision') | Where-Object { Test-Path (Join-Path $SitePackages $_) })
  if ($names.Count -eq 0) { return $false }
  Say "Removing $($names -join ', ') from .venv ..."
  if ($script:UsePip) { & $Py -m pip uninstall -y @names | Out-Host } else { & $script:Uv pip uninstall --python $Py @names | Out-Host }
  return $true
}

# Constraint file pinning the torch / torchvision visible now, so a later install cannot replace them.
function Write-TorchConstraints {
  $cons = Join-Path $env:TEMP 'faravex-torch-constraints.txt'
  & $Py -c "from importlib.metadata import version as v; print('torch==' + v('torch')); print('torchvision==' + v('torchvision'))" |
    Set-Content -Path $cons -Encoding ascii
  return $cons
}

function Repair-Torch([string]$variant) {
  # Common case on a venv that inherits system packages: a torch inside .venv (often a CPU build pulled in as a
  # dependency) hides the working system torch and no longer matches torchvision. Removing it is enough.
  if ($script:Inherit -and (Remove-VenvTorch)) {
    if (-not ((Invoke-Check (Get-TorchWant $variant)) -band 2)) { Say 'torch OK (system copy).'; return $variant }
  }
  $order = @{ 'cu130' = @('cu130', 'cu128', 'cu126', 'cpu'); 'cu128' = @('cu128', 'cu126', 'cpu'); 'cu126' = @('cu126', 'cpu'); 'cpu' = @('cpu') }[$variant]
  if ($Torch -ne 'auto') { $order = @($variant) }
  foreach ($v in $order) {
    $null = Remove-VenvTorch
    # torchvision: the EmbeddingGemma2 processor imports Gemma4's image code even for text. Same index = matching builds.
    Say "Installing torch + torchvision ($v, ~2.5 GB for CUDA builds) ..."
    $idx = @('--index-url', "https://download.pytorch.org/whl/$v")
    if ($script:Inherit) {
      # pip would call the (broken / wrong) system torch "already satisfied": put the new build into .venv
      # explicitly, then a normal install adds whatever torch dependencies are still missing.
      $ok = (Install-Packages (@('--ignore-installed', '--no-deps', 'torch', 'torchvision') + $idx)) -and
            (Install-Packages (@('torch', 'torchvision', '-c', (Write-TorchConstraints)) + $idx))
    } else {
      $ok = Install-Packages (@('torch', 'torchvision') + $idx)
    }
    if ($ok) {
      if (-not ((Invoke-Check (Get-TorchWant $v)) -band 2)) { return $v }
      Warn "torch ($v) installed but cannot use the GPU here; trying the next build."
    }
  }
  Fail 'torch could not be installed. See the messages above.'
}

# ---------- main ----------
Say "Folder: $Root"
$variant = Get-TorchVariant
$venvVer = Get-VenvVersion
$script:Inherit = Test-InheritsSystem

if ($Mode -eq 'check') {
  if (-not $venvVer) { Warn 'No .venv yet.'; exit 1 }
  Say "Python in .venv: $venvVer$(if ($script:Inherit) { ' (also uses system site-packages)' })"
  $code = Invoke-Check (Get-TorchWant $variant) -Show
  if ($code -eq 0) { Say 'Packages: OK' }
  if ($HasModel) { Say "Model: $ModelDir" } else { Warn "Model missing: $ModelDir" }
  exit $code
}

# The real installed state decides what to do, not a marker file: a .venv built by hand counts as set up.
$code = if ($venvVer -and -not $Force) { Invoke-Check (Get-TorchWant $variant) } else { 3 }

if ($code -eq 0) {
  Say 'Environment OK, nothing to install.'
} else {
  $script:Uv = $null
  if ($Force -or -not $venvVer) {
    $script:Uv = Get-Uv
    New-Venv $script:Uv
    $script:Inherit = $false
  } else {
    Say "Repairing existing .venv (Python $venvVer$(if ($script:Inherit) { ', also uses system site-packages' }))."
  }
  $script:UsePip = $script:Inherit
  if (-not $script:UsePip -and -not $script:Uv) { $script:Uv = Get-Uv }
  if (-not $script:Uv) { $script:UsePip = $true }

  if ($code -band 2) { $variant = Repair-Torch $variant }
  if ($variant -eq 'cpu' -and (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) { Warn 'Running on CPU: indexing will be slow.' }

  if ((Invoke-Check 'skip') -band 1) {
    # Pin the torch that now works, so no requirement can swap it for another build (PyPI torch is CPU-only on Windows).
    $cons = Write-TorchConstraints
    Say "Installing missing packages from $(Split-Path -Leaf $Req) ..."
    if (-not (Install-Packages @('-r', $Req, '-c', $cons))) {
      Fail ("Dependency install failed (conflict?). Options: start.bat -Locked   |   start.bat -Python 3.12 -Force   " +
        "| fix requirements.txt yourself, then run start.bat again.")
    }
  }

  if (Invoke-Check (Get-TorchWant $variant)) { Fail 'Packages still not right after install (see above).' }
  if ($HasModel) {
    # Load the model and embed once, so a missing (indirect) dependency fails here, not on first search.
    Say 'Smoke test: loading EmbeddingGemma2 and embedding one line ...'
    Push-Location $Root
    & $Py -c "from faravex import server; v = server._embedder.embed_query('smoke test'); print('[faravex] OK:', len(v), 'dims on', server._embedder._model.device)"
    $smoke = $LASTEXITCODE
    Pop-Location
    if ($smoke -ne 0) { Fail 'Model smoke test failed (see the error above).' }
  }
  Say 'Setup complete.'
  Say 'To use it from Claude Code (MCP), run once:'
  Write-Host '    claude mcp add --transport http --scope user faravex http://127.0.0.1:8766/mcp'
  Write-Host "  (stdio, for clients that start their own server: `"$Py`" `"$(Join-Path $Root 'server.py')`")"
}

if (-not $HasModel) {
  Warn "EmbeddingGemma2 model not found at $ModelDir"
  Warn "Download it (~1.5 GB): & '$Venv\Scripts\hf.exe' download google/embeddinggemma-2 --local-dir '$ModelDir'  (or set FARAVEX_MODEL_PATH)."
  Warn 'The map works without it; indexing, semantic links and the MCP server need it.'
}

if ($Mode -eq 'ui') {
  # MCP (8766) + UI in one process, the same one the Claude Code SessionStart hook starts: reuse it if it runs.
  if (Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue) {
    Say 'faravex already runs (MCP http://127.0.0.1:8766/mcp); opening the UI.'
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$(if ($Port -gt 0) { $Port } else { 8765 })" }
    exit 0
  }
  $uiArgs = @((Join-Path $Root 'server.py'), 'http')
  if ($Port -gt 0) { $uiArgs += @('--ui-port', "$Port") }
  if (-not $NoBrowser) { $uiArgs += '--browser' }
  $host.UI.RawUI.WindowTitle = 'Faravex MCP :8766 + UI :8765'
  & $Py @uiArgs
}

exit $LASTEXITCODE
