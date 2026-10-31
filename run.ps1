# Entry point for people and agents: .\run.ps1 <command> [arguments]
#
# Kept ASCII-only and nearly logic-free. Windows PowerShell 5.1 reads scripts in the system
# code page, drops quotes embedded in native-command arguments and turns redirected stderr
# into errors, so this file only makes sure the project's private Python exists, then hands
# the arguments unchanged to system\bootstrap\launcher.py, which does everything else.
# Where .ps1 files are blocked, run.cmd starts this file with the same arguments.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = $PSScriptRoot
$cliArgs = @($args | ForEach-Object { [string]$_ })

function Join-NativeArguments([string[]]$Items) {
    # Windows hands a program one command-line string; build it the way CommandLineToArgvW splits it.
    ($Items | ForEach-Object {
        if ($_ -ne '' -and $_ -notmatch '[\s"]') { $_; return }
        '"' + (($_ -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
    }) -join ' '
}

function Invoke-Native([string]$File, [string[]]$Arguments, [hashtable]$Environment = @{}) {
    # The child writes straight to this console's stdout/stderr: nothing is re-decoded, and
    # progress on stderr never becomes a PowerShell error. Only the exit code counts.
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $File
    $info.Arguments = Join-NativeArguments $Arguments
    $info.UseShellExecute = $false
    foreach ($key in $Environment.Keys) { $info.EnvironmentVariables[$key] = $Environment[$key] }
    $process = [System.Diagnostics.Process]::Start($info)
    $process.WaitForExit()
    $code = $process.ExitCode
    $process.Dispose()
    $code
}

function Invoke-Launcher([string[]]$Arguments) {
    # Output is read as UTF-8 whatever the console code page, then passed on line by line:
    # stdout lines stay in the PowerShell pipeline (Select-String, ConvertFrom-Json and
    # assignment keep working); stderr lines go to the host as information (shown, and
    # collected by *>&1) so they never become PowerShell errors or mix into parsed output.
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $python
    $info.Arguments = Join-NativeArguments (@('-X', 'utf8', '-I', '-B', '-u', (Join-Path $root 'system\bootstrap\launcher.py')) + $Arguments)
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = New-Object System.Text.UTF8Encoding $false
    $info.StandardErrorEncoding = New-Object System.Text.UTF8Encoding $false
    $process = [System.Diagnostics.Process]::Start($info)
    try {
        $out = $process.StandardOutput.ReadLineAsync()
        $err = $process.StandardError.ReadLineAsync()
        while ($out -or $err) {
            $pending = [System.Threading.Tasks.Task[]]@(@($out, $err) | Where-Object { $_ })
            $done = $pending[[System.Threading.Tasks.Task]::WaitAny($pending)]
            if ([object]::ReferenceEquals($done, $out)) {
                if ($null -eq $out.Result) { $out = $null } else { $out.Result; $out = $process.StandardOutput.ReadLineAsync() }
            } elseif ($null -eq $err.Result) {
                $err = $null
            } else {
                Write-Host $err.Result
                $err = $process.StandardError.ReadLineAsync()
            }
        }
        $process.WaitForExit()
        $script:exitCode = $process.ExitCode
    } finally {
        # A downstream Select-Object -First stops this script early; never leave the launcher
        # blocked on a pipe nobody reads.
        if (-not $process.HasExited) { $process.Kill() }
        $process.Dispose()
    }
}

function Save-PinnedFile([string]$Url, [string]$Path, [string]$Sha256, [bool]$Offline) {
    if ((Test-Path -LiteralPath $Path) -and (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -eq $Sha256) { return }
    if ($Offline) { throw ('Offline setup needs the cached file ' + $Path) }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Path) -Force | Out-Null
    $partial = $Path + '.partial'
    # Windows' own TLS stack fetches missing roots and intermediates on demand; the pinned
    # SHA-256 below is what proves the file, so any transport that delivers it will do.
    $curl = Join-Path $env:SystemRoot 'System32\curl.exe'
    $code = 1
    if (Test-Path -LiteralPath $curl) {
        $code = Invoke-Native $curl @('--fail', '--location', '--silent', '--show-error', '--ssl-revoke-best-effort', '--retry', '3', '--output', $partial, $Url)
    }
    if ($code -ne 0) {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $partial
    }
    if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $Sha256) {
        Remove-Item -LiteralPath $partial -Force
        throw ('Checksum mismatch: ' + $Url)
    }
    Move-Item -LiteralPath $partial -Destination $Path -Force
}

function Install-PrivatePython([bool]$Offline) {
    # First use: uv, then the pinned Python, both inside .runtime. Messages go to the host so
    # that only $true or $false reaches the caller.
    Write-Host 'First run: installing the project-private Python into .runtime (about 50 MB).'
    try {
        $uv = Join-Path $root '.runtime\tools\uv\uv.exe'
        if (-not (Test-Path -LiteralPath $uv)) {
            $zip = Join-Path $root ('.runtime\cache\downloads\' + [IO.Path]::GetFileName($lock.assets.uv.path))
            Save-PinnedFile $lock.assets.uv.url $zip $lock.assets.uv.sha256 $Offline
            Expand-Archive -LiteralPath $zip -DestinationPath (Split-Path -Parent $uv) -Force
        }
        $uvEnv = @{
            UV_PYTHON_INSTALL_DIR = (Join-Path $root '.runtime\python')
            UV_PYTHON_BIN_DIR = (Join-Path $root '.runtime\bin')
            UV_PYTHON_CACHE_DIR = (Join-Path $root '.runtime\cache\python')
            UV_CACHE_DIR = (Join-Path $root '.runtime\cache\uv')
            UV_PYTHON_INSTALL_BIN = 'false'
            UV_PYTHON_INSTALL_REGISTRY = 'false'
            UV_PYTHON_NO_REGISTRY = 'true'
            UV_PYTHON_DOWNLOADS = 'manual'
        }
        $uvArgs = @('--no-config', 'python', 'install', $lock.python)
        if ($Offline) { $uvArgs += '--offline' }
        $code = Invoke-Native $uv $uvArgs $uvEnv
        if ($code -ne 0 -and -not $Offline) {
            # uv trusts its bundled Mozilla roots; a TLS-inspecting proxy's root lives only in the Windows store.
            $uvEnv['UV_SYSTEM_CERTS'] = '1'
            $code = Invoke-Native $uv $uvArgs $uvEnv
        }
        if ($code -ne 0 -or -not (Test-Path -LiteralPath $python)) { throw 'uv could not install the pinned Python.' }
        $true
    } catch {
        Write-Host ('First-run install stopped: ' + $_.Exception.Message)
        Write-Host 'Nothing outside this folder was changed. Check the network connection and run the same command again.'
        $false
    }
}

$exitCode = 1
$consoleEncoding = $null
if ([Console]::IsOutputRedirected) {
    # A program is reading this output (an agent, a pipe to bash): hand it UTF-8 instead of the
    # console code page. This must happen before the first line is written, because a redirected
    # writer keeps the encoding it was created with. The shared console setting is put back below.
    try {
        $consoleEncoding = [Console]::OutputEncoding
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    } catch { $consoleEncoding = $null }
}
try {
    $lock = Get-Content -LiteralPath (Join-Path $root 'system\config\bootstrap-lock.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $python = Join-Path $root ('.runtime\python\cpython-' + $lock.python + '-windows-x86_64-none\python.exe')
    $offline = @($cliArgs | Where-Object { $_ -eq '--offline' -or $_ -eq '-Offline' }).Count -gt 0
    if ((Test-Path -LiteralPath $python) -or (Install-PrivatePython $offline)) {
        Invoke-Launcher $cliArgs
    }
} finally {
    if ($consoleEncoding) { try { [Console]::OutputEncoding = $consoleEncoding } catch { } }
}
exit $exitCode
