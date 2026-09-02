<#
  Configures git to authenticate to GitHub using the PAT in secrets\github_pat.txt.
  The token is stored ONLY in secrets\.git-credentials (gitignored) and is never
  written into the remote URL or any commit.
#>
$ErrorActionPreference = 'Stop'
$repo    = Split-Path -Parent $PSScriptRoot
$patFile = Join-Path $repo 'secrets\github_pat.txt'
$credFile= Join-Path $repo 'secrets\.git-credentials'
$owner   = 'japlie214-dot'

if (-not (Test-Path $patFile)) { throw "Missing $patFile" }
$pat = (Get-Content $patFile -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($pat) -or $pat -eq 'PASTE_YOUR_PAT_HERE') {
    throw "No PAT in $patFile - paste your token there first."
}

# credential store format: https://<user>:<token>@github.com
"https://$($owner):$($pat)@github.com" | Set-Content -Path $credFile -Encoding ascii -NoNewline

# Lock the file down to the current user only.
$acl = Get-Acl $credFile
$acl.SetAccessRuleProtection($true, $false)
$acl.SetAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    "$env:USERDOMAIN\$env:USERNAME", 'FullControl', 'Allow')))
Set-Acl -Path $credFile -AclObject $acl

Push-Location $repo
try {
    git config --local credential.helper "store --file=`"$credFile`""
    git config --local credential.https://github.com.username $owner
    Write-Host "OK. Credential helper configured." -ForegroundColor Green
    Write-Host "Verifying access to the remote..."
    # NOTE: do NOT use --exit-code here. It returns 2 when the remote has no refs,
    # which is exactly what a freshly created empty repo looks like.
    git ls-remote origin *> $null
    if ($LASTEXITCODE -eq 0) { Write-Host "Authenticated to origin successfully." -ForegroundColor Green }
    else { Write-Host "Could not reach origin - check the PAT scope (Contents: Read and write)." -ForegroundColor Yellow }
} finally { Pop-Location }
