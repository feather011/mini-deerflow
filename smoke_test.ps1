<#
.SYNOPSIS
    Mini DeerFlow API Smoke Test - Windows PowerShell 7+
.DESCRIPTION
    验证 Mini DeerFlow API 核心端点可用。
    需要先启动: uv run uvicorn mini_deerflow.api.app:app --port 8000
#>

param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$ApiPrefix = "/api"
)

$ErrorActionPreference = "Stop"

function Invoke-Api {
    param(
        [string]$Method = "GET",
        [string]$Path,
        [hashtable]$Headers = @{},
        [string]$Body = $null,
        [string]$ContentType = $null,
        [string]$UploadFile = $null
    )
    $url = "$BaseUrl$Path"
    $params = @{
        Method = $Method
        Uri = $url
        Headers = $Headers
    }
    if ($Body) {
        $params.Body = $Body
        $params.ContentType = $ContentType
    }
    if ($UploadFile) {
        $fileBytes = [System.IO.File]::ReadAllBytes($UploadFile)
        $fileName = [System.IO.Path]::GetFileName($UploadFile)
        $boundary = [System.Guid]::NewGuid().ToString()
        $params.ContentType = "multipart/form-data; boundary=`"$boundary`""
        $bodyBytes = New-Object System.Collections.Generic.List[byte]
        # file field
        $bodyBytes.AddRange([System.Text.Encoding]::UTF8.GetBytes("--$boundary`r`n"))
        $bodyBytes.AddRange([System.Text.Encoding]::UTF8.GetBytes("Content-Disposition: form-data; name=`"file`"; filename=`"$fileName`"`r`n"))
        $bodyBytes.AddRange([System.Text.Encoding]::UTF8.GetBytes("Content-Type: application/octet-stream`r`n`r`n"))
        $bodyBytes.AddRange($fileBytes)
        $bodyBytes.AddRange([System.Text.Encoding]::UTF8.GetBytes("`r`n--$boundary--`r`n"))
        $params.Body = $bodyBytes.ToArray()
    }
    try {
        $resp = Invoke-RestMethod @params
        $status = 200
    } catch {
        # Invoke-RestMethod throws on non-2xx; get status from exception
        $status = $_.Exception.Response.StatusCode.value__
        $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
        $bodyText = $reader.ReadToEnd()
        $reader.Close()
        try {
            $resp = $bodyText | ConvertFrom-Json -ErrorAction SilentlyContinue
        } catch {
            $resp = @{ raw = $bodyText }
        }
    }
    return @{ Status = $status; Body = $resp; Headers = $_.Exception.Response.Headers }
}

function Assert-Status {
    param([int]$Expected, [int]$Actual, [string]$Context)
    if ($Actual -ne $Expected) {
        Write-Host "[FAIL] $Context : expected $Expected, got $Actual" -ForegroundColor Red
        throw "Assertion failed"
    }
    Write-Host "[PASS] $Context" -ForegroundColor Green
}

Write-Host "=== Mini DeerFlow API Smoke Test ===" -ForegroundColor Cyan
Write-Host "BaseUrl: $BaseUrl" -ForegroundColor Cyan
Write-Host ""

# 1. Health
Write-Host "[1] GET /health" -ForegroundColor Yellow
$r = Invoke-Api -Method GET -Path "$ApiPrefix/health"
Assert-Status 200 $r.Status "health"
Write-Host "     $($r.Body | ConvertTo-Json)" -ForegroundColor DarkGray

# 2. Ready
Write-Host "[2] GET /ready" -ForegroundColor Yellow
$r = Invoke-Api -Method GET -Path "$ApiPrefix/ready"
Assert-Status 200 $r.Status "ready"
Write-Host "     $($r.Body | ConvertTo-Json)" -ForegroundColor DarkGray

# 3. Create Thread
Write-Host "[3] POST /threads" -ForegroundColor Yellow
$r = Invoke-Api -Method POST -Path "$ApiPrefix/threads" `
    -Headers @{"Content-Type" = "application/json"} `
    -Body '{"thread_id": "smoke-thread"}'
Assert-Status 201 $r.Status "create thread"
$threadId = $r.Body.thread_id
Write-Host "     thread_id: $threadId" -ForegroundColor DarkGray

# 4. List Threads
Write-Host "[4] GET /threads" -ForegroundColor Yellow
$r = Invoke-Api -Method GET -Path "$ApiPrefix/threads"
Assert-Status 200 $r.Status "list threads"
Write-Host "     count: $($r.Body.threads.Count)" -ForegroundColor DarkGray

# 5. Upload File (via curl-compatible multipart)
Write-Host "[5] POST /files (upload)" -ForegroundColor Yellow
# 创建临时测试文件
$testFile = [System.IO.Path]::GetTempFileName() + ".txt"
[System.IO.File]::WriteAllText($testFile, "Hello from smoke test!", [System.Text.Encoding]::UTF8)
$r = Invoke-Api -Method POST -Path "$ApiPrefix/files" -UploadFile $testFile
Assert-Status 201 $r.Status "upload file"
Write-Host "     name: $($r.Body.name), size: $($r.Body.size)" -ForegroundColor DarkGray
$uploadedName = $r.Body.name
Remove-Item $testFile -ErrorAction SilentlyContinue

# 6. List Files
Write-Host "[6] GET /files" -ForegroundColor Yellow
$r = Invoke-Api -Method GET -Path "$ApiPrefix/files"
Assert-Status 200 $r.Status "list files"
$found = $false
foreach ($entry in $r.Body.entries) {
    if ($entry.name -eq $uploadedName) {
        $found = $true
        break
    }
}
if ($found) { Write-Host "[PASS] uploaded file appears in list" -ForegroundColor Green }
else { Write-Host "[FAIL] uploaded file not in list" -ForegroundColor Red; throw "uploaded file not found" }

# 7. Download File
Write-Host "[7] GET /files/$uploadedName" -ForegroundColor Yellow
$r = Invoke-Api -Method GET -Path "$ApiPrefix/files/$uploadedName"
Assert-Status 200 $r.Status "download file"
Write-Host "     content length: $($r.Body.raw.Length)" -ForegroundColor DarkGray

# 8. Download Artifact (should be empty/404)
Write-Host "[8] GET /artifacts (expect empty)" -ForegroundColor Yellow
$r = Invoke-Api -Method GET -Path "$ApiPrefix/artifacts"
Assert-Status 200 $r.Status "list artifacts"
Write-Host "     entries: $($r.Body.entries.Count)" -ForegroundColor DarkGray

# 9. Delete Upload
Write-Host "[9] DELETE /files/$uploadedName" -ForegroundColor Yellow
$r = Invoke-Api -Method DELETE -Path "$ApiPrefix/files/$uploadedName"
Assert-Status 204 $r.Status "delete file"

# 10. Verify Deleted
Write-Host "[10] GET /files/$uploadedName (expect 404)" -ForegroundColor Yellow
$r = Invoke-Api -Method GET -Path "$ApiPrefix/files/$uploadedName"
Assert-Status 404 $r.Status "file deleted (404)"

# 11. Delete Thread
Write-Host "[11] DELETE /threads/$threadId" -ForegroundColor Yellow
$r = Invoke-Api -Method DELETE -Path "$ApiPrefix/threads/$threadId"
Assert-Status 204 $r.Status "delete thread"

# 12. Verify Thread Deleted
Write-Host "[12] GET /threads/$threadId (expect 404)" -ForegroundColor Yellow
$r = Invoke-Api -Method GET -Path "$ApiPrefix/threads/$threadId"
Assert-Status 404 $r.Status "thread deleted (404)"

# 13. X-Request-ID header check
Write-Host "[13] X-Request-ID header present" -ForegroundColor Yellow
$r = Invoke-Api -Method GET -Path "$ApiPrefix/health"
$reqId = $r.Headers["X-Request-ID"]
if ($reqId) {
    Write-Host "[PASS] X-Request-ID: $reqId" -ForegroundColor Green
} else {
    Write-Host "[FAIL] X-Request-ID header missing" -ForegroundColor Red
    throw "X-Request-ID missing"
}

Write-Host ""
Write-Host "=== All Smoke Tests Passed ===" -ForegroundColor Cyan
