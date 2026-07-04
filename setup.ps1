<#
  Disk-Rip one-time setup for Windows.

  Installs the prerequisites (Python, MakeMKV, ffmpeg-with-libbluray) via winget,
  finds their executables, and writes config.json.

  Usage (double-clickable via setup.cmd, or):
    powershell -ExecutionPolicy Bypass -File setup.ps1

  Unattended / re-run with values supplied:
    powershell -ExecutionPolicy Bypass -File setup.ps1 -TmdbKey abc123 `
        -TvRoot \\SERVER\media\tv -MovieRoot \\SERVER\media\movies

  Re-running is safe: existing config.json values are used as the defaults.
#>
param(
  [string]$TmdbKey,
  [string]$TvRoot,
  [string]$MovieRoot,
  [string]$WorkDir,
  [string]$MakeMkvKey,
  [string]$ConfigPath,
  [switch]$SkipInstall,
  [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ConfigPath) { $ConfigPath = Join-Path $here 'config.json' }
$examplePath = Join-Path $here 'config.example.json'

function Info($m)  { Write-Host $m -ForegroundColor Cyan }
function Ok($m)    { Write-Host "  OK  $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "  !   $m" -ForegroundColor Yellow }
function Fail($m)  { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

# --- winget ----------------------------------------------------------------
function Test-Cmd($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

function Install-Pkg($id, $friendly, $probeCmd) {
  if ($probeCmd -and (Test-Cmd $probeCmd)) { Ok "$friendly already installed"; return }
  $listed = winget list --id $id -e --accept-source-agreements 2>$null | Select-String $id
  if ($listed) { Ok "$friendly already installed"; return }
  Info "Installing $friendly ($id) ..."
  winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
  if ($LASTEXITCODE -ne 0) { Warn "winget returned $LASTEXITCODE for $friendly - continuing (it may already be present)" }
  else { Ok "$friendly installed" }
}

# --- locate executables ----------------------------------------------------
function Find-MakeMkvCon {
  $candidates = @(
    'C:\Program Files (x86)\MakeMKV\makemkvcon64.exe',
    'C:\Program Files\MakeMKV\makemkvcon64.exe'
  )
  foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
  $found = Get-ChildItem 'C:\Program Files*','C:\Program Files (x86)*' -Recurse -Filter 'makemkvcon64.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($found) { return $found.FullName }
  return $null
}

function Find-Ffmpeg {
  # winget (Gyan.FFmpeg) installs per-user under WinGet\Packages
  $glob = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*-full_build\bin\ffmpeg.exe'
  $found = Get-ChildItem $glob -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
  if ($found) { return $found.FullName }
  $cmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  return $null
}

# --- config helpers --------------------------------------------------------
function Load-Json($path) {
  if (Test-Path $path) { return Get-Content $path -Raw | ConvertFrom-Json }
  return $null
}
function Prompt-Default($label, $current) {
  if ($NonInteractive) { return $current }
  if ($current) { $ans = Read-Host "$label [$current]" } else { $ans = Read-Host $label }
  if ([string]::IsNullOrWhiteSpace($ans)) { return $current } else { return $ans.Trim() }
}

# ===========================================================================
Info "`n=== Disk-Rip setup ===`n"

if (-not $SkipInstall) {
  if (-not (Test-Cmd 'winget')) {
    Fail "winget not found. Update 'App Installer' from the Microsoft Store (Windows 10 1809+/11), then re-run."
  }
  Install-Pkg 'Python.Python.3.12' 'Python 3'          'py'
  Install-Pkg 'GuinpinSoft.MakeMKV' 'MakeMKV'          $null
  Install-Pkg 'Gyan.FFmpeg'         'ffmpeg (full/libbluray)' $null
} else {
  Warn "SkipInstall set - not running winget"
}

# --- locate ---------------------------------------------------------------
Info "`nLocating executables ..."
$makemkv = Find-MakeMkvCon
if ($makemkv) { Ok "makemkvcon: $makemkv" } else { Warn "makemkvcon64.exe not found - set 'makemkvcon' in config.json manually" }
$ffmpeg = Find-Ffmpeg
if ($ffmpeg) { Ok "ffmpeg: $ffmpeg" } else { Warn "ffmpeg.exe not found - thumbnails will be off until you set 'ffmpeg' in config.json" }

# --- build config ---------------------------------------------------------
Info "`nConfiguring ..."
$example = Load-Json $examplePath
$existing = Load-Json $ConfigPath
if (-not $example) { Fail "config.example.json not found next to setup.ps1" }

# start from example (all keys), overlay existing values so nothing is lost
$cfg = [ordered]@{}
foreach ($p in $example.PSObject.Properties) { $cfg[$p.Name] = $p.Value }
if ($existing) { foreach ($p in $existing.PSObject.Properties) { $cfg[$p.Name] = $p.Value } }

# resolve values: parameter > existing/prompt > located path
function Cur($key) { if ($cfg.Contains($key)) { return $cfg[$key] } else { return $null } }
$curKey = Cur 'tmdb_api_key'; if ($curKey -like 'PASTE_*') { $curKey = '' }

if ($TmdbKey)   { $cfg['tmdb_api_key'] = $TmdbKey } else { $cfg['tmdb_api_key'] = Prompt-Default 'TMDB v3 API key (themoviedb.org)' $curKey }
if ($TvRoot)    { $cfg['tv_root']    = $TvRoot }    else { $cfg['tv_root']    = Prompt-Default 'TV library path'    (Cur 'tv_root') }
if ($MovieRoot) { $cfg['movie_root'] = $MovieRoot } else { $cfg['movie_root'] = Prompt-Default 'Movie library path' (Cur 'movie_root') }
$defWork = if (Cur 'work_dir') { Cur 'work_dir' } else { Join-Path $here '_work' }
if ($WorkDir)   { $cfg['work_dir']   = $WorkDir }   else { $cfg['work_dir']   = Prompt-Default 'Local scratch/work dir' $defWork }
if ($makemkv)   { $cfg['makemkvcon'] = $makemkv }
if ($ffmpeg)    { $cfg['ffmpeg']     = $ffmpeg }

# write config.json - WITHOUT a BOM (PS 5.1's -Encoding utf8 adds one, which
# breaks Python's json.loads)
$json = $cfg | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText($ConfigPath, $json, (New-Object System.Text.UTF8Encoding($false)))
Ok "wrote $ConfigPath"

# --- MakeMKV registration -------------------------------------------------
if ($makemkv) {
  if (-not $MakeMkvKey -and -not $NonInteractive) {
    Write-Host ""
    Warn "MakeMKV needs a license key (paid) or the free BETA key (rotates monthly, posted at forum.makemkv.com/forum/viewtopic.php?t=1053)."
    $MakeMkvKey = Read-Host "Paste a MakeMKV key to register now (or press Enter to do it later in the MakeMKV app)"
  }
  if ($MakeMkvKey) {
    & $makemkv reg $MakeMkvKey | Out-Null
    if ($LASTEXITCODE -eq 0) { Ok "MakeMKV registered" } else { Warn "MakeMKV registration failed - open the MakeMKV app and enter the key manually" }
  }
}

# --- verify ---------------------------------------------------------------
Info "`nVerifying ..."
if ($ffmpeg) {
  $proto = & $ffmpeg -hide_banner -protocols 2>$null | Select-String -SimpleMatch 'bluray'
  if ($proto) { Ok "ffmpeg has the bluray protocol (thumbnails supported)" }
  else { Warn "this ffmpeg lacks libbluray - thumbnails won't decrypt discs. Install the Gyan 'full' build." }
}
$key = $cfg['tmdb_api_key']
if ($key -and $key -notlike 'PASTE_*') {
  try {
    $r = Invoke-RestMethod "https://api.themoviedb.org/3/movie/550?api_key=$key" -TimeoutSec 20
    if ($r.title) { Ok "TMDB key works (test lookup: '$($r.title)')" }
  } catch { Warn "TMDB key test failed - double-check the key (v3 auth) at themoviedb.org/settings/api" }
} else { Warn "no TMDB key set - add it to config.json before running" }

Info "`n=== Done ===`n"
Write-Host "Next:" -ForegroundColor Cyan
Write-Host "  1. If MakeMKV isn't registered yet, open it once and enter your key."
Write-Host "  2. Close the MakeMKV window (it holds the drive), insert a disc, then run:"
Write-Host "       py webapp.py        # web UI (drag-drop matching)" -ForegroundColor White
Write-Host "       py diskrip.py       # or the terminal version" -ForegroundColor White
