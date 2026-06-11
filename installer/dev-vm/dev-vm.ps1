#Requires -Version 5.1
<#
.SYNOPSIS
  Hosty dev VM driver for Windows hosts (macOS/Linux: dev-vm.sh).
.DESCRIPTION
  Launches and manages the hosty-dev Multipass VM (Ubuntu 24.04), mounts the
  repo at /home/ubuntu/hosty, and runs provisioning/backend/tests inside it.
.EXAMPLE
  .\installer\dev-vm\dev-vm.ps1 up
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet("up", "provision", "backend", "test", "smoke", "shell", "ip", "status", "down", "destroy")]
    [string]$Command = "up"
)

$ErrorActionPreference = "Stop"
$VM = "hosty-dev"
$Mount = "/home/ubuntu/hosty"
$RepoDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

if (-not (Get-Command multipass -ErrorAction SilentlyContinue)) {
    throw "Multipass not found - install from https://canonical.com/multipass"
}

function Invoke-VM([string]$Script) {
    multipass exec $VM -- sudo bash -lc $Script
    if ($LASTEXITCODE -ne 0) { throw "Command failed in VM (exit $LASTEXITCODE)" }
}

function Get-VMIp {
    $m = (multipass info $VM | Out-String) | Select-String 'IPv4:\s+(\S+)'
    if (-not $m) { throw "Could not determine VM IP - is $VM running?" }
    $m.Matches[0].Groups[1].Value
}

function Test-VMExists {
    # NB: avoid `*> $null` here - with ErrorActionPreference=Stop, Windows
    # PowerShell 5.1 turns native stderr into a terminating NativeCommandError.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    multipass info $VM 2>&1 | Out-Null
    $ErrorActionPreference = $prev
    return ($LASTEXITCODE -eq 0)
}

switch ($Command) {
    "up" {
        # Mounting host folders on Windows requires privileged mounts.
        multipass set local.privileged-mounts=true
        if (-not (Test-VMExists)) {
            multipass launch 24.04 --name $VM --cpus 2 --memory 4G --disk 20G `
                --cloud-init (Join-Path $PSScriptRoot "cloud-init.yaml")
            if ($LASTEXITCODE -ne 0) { throw "multipass launch failed" }
        }
        else {
            multipass start $VM
        }
        $info = multipass info $VM | Out-String
        if ($info -notmatch [regex]::Escape($Mount)) {
            multipass mount $RepoDir "${VM}:$Mount"
            if ($LASTEXITCODE -ne 0) { throw "multipass mount failed" }
        }
        Invoke-VM "bash $Mount/installer/provision.sh"
        Invoke-VM "bash $Mount/installer/dev-vm/vm-setup.sh"
        $ip = Get-VMIp
        Write-Host ""
        Write-Host "VM ready. IP: $ip"
        Write-Host "Next: .\installer\dev-vm\dev-vm.ps1 backend  ->  http://${ip}:8800/api/docs"
    }
    "provision" {
        Invoke-VM "bash $Mount/installer/provision.sh && bash $Mount/installer/dev-vm/vm-setup.sh"
    }
    "backend" {
        $ip = Get-VMIp
        Write-Host "API -> http://${ip}:8800  (Ctrl+C stops it)"
        Invoke-VM "set -a; . /var/lib/hosty/dev.env; set +a; cd $Mount/backend && UV_PROJECT_ENVIRONMENT=/var/lib/hosty/venv exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8800"
    }
    "test" {
        Invoke-VM "set -a; . /var/lib/hosty/dev.env; set +a; cd $Mount/backend && UV_PROJECT_ENVIRONMENT=/var/lib/hosty/venv uv run pytest -m vm"
    }
    "smoke" { Invoke-VM "bash $Mount/installer/dev-vm/smoke.sh" }
    "shell" { multipass shell $VM }
    "ip" { Get-VMIp }
    "status" { multipass info $VM }
    "down" { multipass stop $VM }
    "destroy" { multipass delete --purge $VM }
}
