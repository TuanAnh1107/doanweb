param(
    [string]$ApiKey = $env:RENDER_API_KEY,
    [string]$OwnerId = $env:RENDER_OWNER_ID,
    [string]$ServiceName = "doanweb",
    [string]$RepoUrl = "https://github.com/TuanAnh1107/doanweb",
    [string]$Branch = "main",
    [switch]$WaitForLive = $true
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    throw "Missing Render API key. Set RENDER_API_KEY or pass -ApiKey."
}

$headers = @{
    Authorization = "Bearer $ApiKey"
    Accept = "application/json"
    "Content-Type" = "application/json"
}

function Invoke-RenderApi {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("GET", "POST", "PATCH", "DELETE")] [string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $false)][object]$Body
    )

    $uri = "https://api.render.com/v1$Path"

    try {
        if ($PSBoundParameters.ContainsKey("Body")) {
            $jsonBody = $Body | ConvertTo-Json -Depth 30 -Compress
            return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers -Body $jsonBody
        }

        return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers
    }
    catch {
        if ($_.Exception.Response -and $_.Exception.Response.GetResponseStream()) {
            $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
            $responseBody = $reader.ReadToEnd()
            throw "Render API $Method $Path failed: $responseBody"
        }

        throw
    }
}

function Normalize-ServiceRecords {
    param([object]$ApiResult)

    $items = @($ApiResult)
    $normalized = @()
    foreach ($item in $items) {
        if ($null -ne $item.service) {
            $normalized += $item.service
        }
        else {
            $normalized += $item
        }
    }

    return $normalized
}

function Normalize-OwnerRecords {
    param([object]$ApiResult)

    $items = @($ApiResult)
    $normalized = @()
    foreach ($item in $items) {
        if ($null -ne $item.owner) {
            $normalized += $item.owner
        }
        else {
            $normalized += $item
        }
    }

    return $normalized
}

if ([string]::IsNullOrWhiteSpace($OwnerId)) {
    $ownersResponse = Invoke-RenderApi -Method GET -Path "/owners?limit=20"
    $owners = Normalize-OwnerRecords -ApiResult $ownersResponse

    $selectedOwner = $owners | Where-Object { $_.type -eq "user" } | Select-Object -First 1
    if (-not $selectedOwner) {
        $selectedOwner = $owners | Select-Object -First 1
    }

    if (-not $selectedOwner) {
        throw "No Render owner/workspace found for this API key."
    }

    $OwnerId = $selectedOwner.id
}

Write-Host "Using ownerId: $OwnerId"

$encodedServiceName = [System.Uri]::EscapeDataString($ServiceName)
$servicesResponse = Invoke-RenderApi -Method GET -Path "/services?ownerId=$OwnerId&name=$encodedServiceName&type=web_service&limit=20"
$services = Normalize-ServiceRecords -ApiResult $servicesResponse
$existingService = $services | Where-Object { $_.name -eq $ServiceName -and $_.type -eq "web_service" } | Select-Object -First 1

$service = $null
$deployId = $null

if ($existingService) {
    Write-Host "Service '$ServiceName' exists. Triggering a new deploy..."
    $service = $existingService
    $deployResponse = Invoke-RenderApi -Method POST -Path "/services/$($service.id)/deploys" -Body @{
        clearCache = "do_not_clear"
    }
    $deployId = $deployResponse.id
}
else {
    Write-Host "Creating new service '$ServiceName'..."
    $createPayload = @{
        type       = "web_service"
        name       = $ServiceName
        ownerId    = $OwnerId
        repo       = $RepoUrl
        autoDeploy = "yes"
        branch     = $Branch
        envVars    = @(
            @{
                key           = "SECRET_KEY"
                generateValue = $true
            },
            @{
                key   = "FLASK_DEBUG"
                value = "false"
            },
            @{
                key   = "DB_ENGINE"
                value = "sqlite"
            },
            @{
                key   = "SQLITE_PATH"
                value = "/tmp/quanlylophoc.db"
            }
        )
        serviceDetails = @{
            runtime         = "python"
            plan            = "free"
            region          = "singapore"
            healthCheckPath = "/login"
            envSpecificDetails = @{
                buildCommand = "pip install -r requirements.txt"
                startCommand = 'gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120'
            }
        }
    }

    $createResponse = Invoke-RenderApi -Method POST -Path "/services" -Body $createPayload
    $service = $createResponse.service
    $deployId = $createResponse.deployId
}

if ([string]::IsNullOrWhiteSpace($deployId)) {
    throw "No deployId returned from Render."
}

Write-Host "Deploy started. deployId: $deployId"

if ($WaitForLive) {
    $terminalStatuses = @("live", "build_failed", "update_failed", "pre_deploy_failed", "canceled", "deactivated")

    while ($true) {
        Start-Sleep -Seconds 10
        $deploy = Invoke-RenderApi -Method GET -Path "/services/$($service.id)/deploys/$deployId"
        Write-Host ("Current deploy status: " + $deploy.status)

        if ($terminalStatuses -contains $deploy.status) {
            if ($deploy.status -ne "live") {
                throw "Deploy ended with status: $($deploy.status)"
            }

            break
        }
    }
}

$service = Invoke-RenderApi -Method GET -Path "/services/$($service.id)"
$serviceUrl = $service.serviceDetails.url
$dashboardUrl = $service.dashboardUrl

Write-Host ""
Write-Host "Render service is live."
Write-Host "URL: $serviceUrl"
Write-Host "Dashboard: $dashboardUrl"
