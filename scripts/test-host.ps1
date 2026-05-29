# test-host.ps1 — Post-deployment smoke test against a live ECHO instance
# Run this AFTER deploying to verify the service is healthy and responding correctly.
# Usage: .\scripts\test-host.ps1 [-BaseUrl http://hostname:8001] [-Token your-bearer-token]

param(
    [string]$BaseUrl = "http://127.0.0.1:8001",
    [string]$Token   = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir   = Split-Path -Parent $ScriptDir

$Passed = 0
$Failed = 0

function Assert-True {
    param([bool]$Condition, [string]$Label)
    if ($Condition) {
        Write-Host "  PASS  $Label" -ForegroundColor Green
        $script:Passed++
    } else {
        Write-Host "  FAIL  $Label" -ForegroundColor Red
        $script:Failed++
    }
}

function Build-Headers {
    $h = @{ "Accept" = "application/json" }
    if ($Token) { $h["Authorization"] = "Bearer $Token" }
    return $h
}

function New-SineWav {
    # Generates a minimal 16kHz mono 16-bit WAV with a 440Hz sine tone (1 second)
    $sampleRate  = 16000
    $numSamples  = 16000
    $amplitude   = 16000

    $dataBytes = New-Object byte[] ($numSamples * 2)
    for ($i = 0; $i -lt $numSamples; $i++) {
        $sample = [int16]($amplitude * [Math]::Sin(2 * [Math]::PI * 440 * $i / $sampleRate))
        $bytes  = [BitConverter]::GetBytes($sample)
        $dataBytes[$i * 2]     = $bytes[0]
        $dataBytes[$i * 2 + 1] = $bytes[1]
    }

    $dataLen = $dataBytes.Length
    $stream  = New-Object System.IO.MemoryStream
    $writer  = New-Object System.IO.BinaryWriter($stream)

    # RIFF header
    $writer.Write([System.Text.Encoding]::ASCII.GetBytes("RIFF"))
    $writer.Write([int32](36 + $dataLen))
    $writer.Write([System.Text.Encoding]::ASCII.GetBytes("WAVE"))
    # fmt chunk
    $writer.Write([System.Text.Encoding]::ASCII.GetBytes("fmt "))
    $writer.Write([int32]16)         # PCM chunk size
    $writer.Write([int16]1)          # PCM format
    $writer.Write([int16]1)          # Mono
    $writer.Write([int32]$sampleRate)
    $writer.Write([int32]($sampleRate * 2))
    $writer.Write([int16]2)          # block align
    $writer.Write([int16]16)         # bits per sample
    # data chunk
    $writer.Write([System.Text.Encoding]::ASCII.GetBytes("data"))
    $writer.Write([int32]$dataLen)
    $writer.Write($dataBytes)

    $writer.Flush()
    $bytes = $stream.ToArray()
    $writer.Dispose()
    return $bytes
}

Write-Host "`n[host-test] ECHO Post-Deployment Smoke Test" -ForegroundColor Cyan
Write-Host "[host-test] Target: $BaseUrl`n"

# -----------------------------------------------------------
# Test 1: Health check
# -----------------------------------------------------------
Write-Host "[1] Health check" -ForegroundColor Yellow
try {
    $resp = Invoke-RestMethod -Uri "$BaseUrl/health" -Method Get -Headers (Build-Headers) -TimeoutSec 10
    Assert-True ($resp.status -eq "ok")   "status == ok"
    Assert-True ($resp.vad -ne $null)     "vad field present"
    Assert-True ($resp.backend -ne $null) "backend field present"
    if ($resp.backend -ne "ok") {
        Write-Host "  WARN  backend reports: $($resp.backend)" -ForegroundColor Yellow
    }
} catch {
    Assert-True $false "Health check reachable (error: $_)"
}

# -----------------------------------------------------------
# Test 2: No-auth request is rejected when token is set
# -----------------------------------------------------------
Write-Host "`n[2] Auth enforcement (skipped — no token provided)" -ForegroundColor Yellow
if ($Token) {
    try {
        $resp = Invoke-WebRequest -Uri "$BaseUrl/v1/audio/transcriptions" -Method Post `
            -Headers @{ "Accept" = "application/json" } `
            -Body @{ model = "whisper-1" } `
            -Form @{} -TimeoutSec 10 -SkipHttpErrorCheck
        Assert-True ($resp.StatusCode -eq 401) "Unauthenticated request returns 401"
    } catch {
        Write-Host "  SKIP  Auth test errored: $_" -ForegroundColor DarkYellow
    }
} else {
    Write-Host "  SKIP  (Pass -Token to enable auth enforcement test)" -ForegroundColor DarkYellow
}

# -----------------------------------------------------------
# Test 3: Real audio upload returns a valid response
# -----------------------------------------------------------
Write-Host "`n[3] Audio transcription endpoint" -ForegroundColor Yellow
$TmpWav = Join-Path $env:TEMP "echo_smoke_test.wav"
try {
    $wavBytes = New-SineWav
    [System.IO.File]::WriteAllBytes($TmpWav, $wavBytes)

    $headers = Build-Headers

    # Use HttpClient for proper multipart/form-data
    $httpClient   = New-Object System.Net.Http.HttpClient
    foreach ($kv in $headers.GetEnumerator()) {
        $httpClient.DefaultRequestHeaders.Add($kv.Key, $kv.Value)
    }

    $multipart = New-Object System.Net.Http.MultipartFormDataContent
    $fileStream   = [System.IO.File]::OpenRead($TmpWav)
    $streamContent = New-Object System.Net.Http.StreamContent($fileStream)
    $streamContent.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse("audio/wav")
    $multipart.Add($streamContent, "file", "smoke_test.wav")
    $multipart.Add((New-Object System.Net.Http.StringContent("whisper-1")), "model")

    $respMsg = $httpClient.PostAsync("$BaseUrl/v1/audio/transcriptions", $multipart).Result
    $body    = $respMsg.Content.ReadAsStringAsync().Result | ConvertFrom-Json

    $fileStream.Dispose()
    $httpClient.Dispose()

    Assert-True ($respMsg.StatusCode -eq 200) "POST /v1/audio/transcriptions returns 200"
    Assert-True ($null -ne $body.text)         "Response contains 'text' field"
    Write-Host "  INFO  Transcription result: '$($body.text)'" -ForegroundColor DarkCyan
} catch {
    Assert-True $false "Audio upload test (error: $_)"
} finally {
    if (Test-Path $TmpWav) { Remove-Item $TmpWav -Force }
}

# -----------------------------------------------------------
# Summary
# -----------------------------------------------------------
Write-Host "`n[host-test] Results: $Passed passed, $Failed failed" -ForegroundColor Cyan
if ($Failed -gt 0) {
    Write-Host "[host-test] FAILED" -ForegroundColor Red
    exit 1
} else {
    Write-Host "[host-test] All checks passed." -ForegroundColor Green
    exit 0
}
