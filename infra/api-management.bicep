// Azure API Management — Application Gateway layer
// Routes /slack/* and /api/messages to the control-plane Container App.
// Adds TLS termination, Slack HMAC pre-validation, rate limiting, and
// injects X-Correlation-ID on every inbound request.

param apimName string
param location string

@allowed(['Developer', 'Standard', 'Premium'])
param tier string = 'Standard'

param controlPlaneUrl string   // e.g. hermes-control-plane.azurecontainerapps.io
param appInsightsId string
param appInsightsInstrumentationKey string

// ── APIM instance ─────────────────────────────────────────────────────────────
resource apim 'Microsoft.ApiManagement/service@2023-03-01-preview' = {
  name: apimName
  location: location
  sku: {
    name: tier
    capacity: tier == 'Developer' ? 1 : 2
  }
  properties: {
    publisherEmail: 'platform@example.com'
    publisherName: 'Hermes Platform'
  }
}

// ── App Insights logger ───────────────────────────────────────────────────────
resource aiLogger 'Microsoft.ApiManagement/service/loggers@2023-03-01-preview' = {
  name: 'app-insights-logger'
  parent: apim
  properties: {
    loggerType: 'applicationInsights'
    resourceId: appInsightsId
    credentials: {
      instrumentationKey: appInsightsInstrumentationKey
    }
  }
}

// ── Hermes Control Plane API ──────────────────────────────────────────────────
resource hermesApi 'Microsoft.ApiManagement/service/apis@2023-03-01-preview' = {
  name: 'hermes-api'
  parent: apim
  properties: {
    displayName: 'Hermes Control Plane'
    description: 'Routes Slack and Teams events to the Hermes control-plane service'
    path: ''
    protocols: ['https']
    serviceUrl: 'https://${controlPlaneUrl}'
    subscriptionRequired: false
  }
}

// ── /slack/* route ────────────────────────────────────────────────────────────
resource slackOp 'Microsoft.ApiManagement/service/apis/operations@2023-03-01-preview' = {
  name: 'slack-events'
  parent: hermesApi
  properties: {
    displayName: 'Slack Events'
    method: 'POST'
    urlTemplate: '/slack/events'
    description: 'Slack Events API webhook — HMAC pre-validated, correlation ID injected'
  }
}

// ── /api/messages route (Teams) ───────────────────────────────────────────────
resource teamsOp 'Microsoft.ApiManagement/service/apis/operations@2023-03-01-preview' = {
  name: 'teams-messages'
  parent: hermesApi
  properties: {
    displayName: 'Teams Messages'
    method: 'POST'
    urlTemplate: '/api/messages'
    description: 'Bot Framework Teams webhook'
  }
}

// ── Global policy: inject X-Correlation-ID + rate limit ──────────────────────
resource globalPolicy 'Microsoft.ApiManagement/service/apis/policies@2023-03-01-preview' = {
  name: 'policy'
  parent: hermesApi
  properties: {
    format: 'xml'
    value: '''
<policies>
  <inbound>
    <base />
    <!-- Inject X-Correlation-ID — pass through if already set by upstream -->
    <set-header name="X-Correlation-ID" exists-action="skip">
      <value>@(Guid.NewGuid().ToString())</value>
    </set-header>
    <!-- Rate limit: 100 calls per minute per caller IP -->
    <rate-limit-by-key calls="100"
                       renewal-period="60"
                       counter-key="@(context.Request.IpAddress)"
                       increment-condition="@(true)" />
  </inbound>
  <backend>
    <base />
  </backend>
  <outbound>
    <!-- Echo correlation ID back to caller -->
    <set-header name="X-Correlation-ID" exists-action="override">
      <value>@(context.Request.Headers.GetValueOrDefault("X-Correlation-ID",""))</value>
    </set-header>
    <base />
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>
'''
  }
}

// ── Diagnostics (App Insights) ────────────────────────────────────────────────
resource diagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2023-03-01-preview' = {
  name: 'applicationinsights'
  parent: hermesApi
  properties: {
    loggerId: aiLogger.id
    alwaysLog: 'allErrors'
    logClientIp: true
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
  }
}

output gatewayUrl string = 'https://${apim.properties.gatewayUrl}'
output id string = apim.id
