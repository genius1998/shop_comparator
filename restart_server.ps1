# Restart only this project's web server (port 8765).
Set-Location $PSScriptRoot
$conn = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
foreach ($c in $conn) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)"
    if ($p.CommandLine -match 'main\.py') { Stop-Process -Id $p.ProcessId -Confirm:$false }
    else { Write-Host "Port 8765 is used by another program: $($p.Name)"; exit 1 }
}
New-Item -ItemType Directory -Force logs | Out-Null
Start-Process -FilePath .\.venv\Scripts\python.exe -ArgumentList 'main.py' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
    -RedirectStandardOutput logs\server.out.log -RedirectStandardError logs\server.err.log
Start-Sleep 3
try { (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/api/state -TimeoutSec 8).StatusCode }
catch { Write-Host "Server did not start. See logs\server.err.log"; exit 1 }
