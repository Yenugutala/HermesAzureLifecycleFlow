// Azure Container App — shared module for control-plane and hermes-service
param appName string
param location string
param containerEnvId string
param acrLoginServer string
param acrAdminUsername string
@secure()
param acrAdminPassword string
param imageName string
param imageTag string = 'latest'
param minReplicas int = 0
param maxReplicas int = 3

// HTTP port — set to 0 for queue-consumer apps (no ingress)
param httpPort int = 3978

// Queue-consumer mode (hermes-service): no HTTP ingress, scales on SB depth
param isQueueConsumer bool = false
param sbNamespaceName string = ''
param sbQueueName string = 'control-plane-queue'

// Azure Files volume for SessionDB persistence
param stateVolumeName string = ''

param env array = []
param secrets array = []

// ── Scale rules ───────────────────────────────────────────────────────────────
var httpScaleRule = {
  name: 'http-scale'
  http: {
    metadata: {
      concurrentRequests: '20'
    }
  }
}

var sbScaleRule = {
  name: 'sb-queue-scale'
  custom: {
    type: 'azure-servicebus'
    metadata: {
      queueName: sbQueueName
      namespace: sbNamespaceName
      messageCount: '5'    // 1 replica per 5 queued messages
    }
    auth: [
      {
        secretRef: 'sb-connection-string'
        triggerParameter: 'connection'
      }
    ]
  }
}

var scaleRules = isQueueConsumer ? [sbScaleRule] : [httpScaleRule]

// ── Volumes ────────────────────────────────────────────────────────────────────
var volumes = stateVolumeName != '' ? [
  {
    name: stateVolumeName
    storageName: stateVolumeName
    storageType: 'AzureFile'
  }
] : []

var volumeMounts = stateVolumeName != '' ? [
  {
    volumeName: stateVolumeName
    mountPath: '/mnt/hermes-state'
  }
] : []

// ── Container App ─────────────────────────────────────────────────────────────
resource app 'Microsoft.App/containerApps@2023-05-01' = {
  name: appName
  location: location
  properties: {
    environmentId: containerEnvId
    configuration: {
      ingress: isQueueConsumer ? null : {
        external: true
        targetPort: httpPort
        transport: 'http'
      }
      registries: [
        {
          server: acrLoginServer
          username: acrAdminUsername
          passwordSecretRef: 'acr-password'
        }
      ]
      secrets: concat(secrets, [
        { name: 'acr-password', value: acrAdminPassword }
      ])
    }
    template: {
      containers: [
        {
          name: appName
          image: '${acrLoginServer}/${imageName}:${imageTag}'
          resources: {
            cpu: '0.5'
            memory: '1Gi'
          }
          env: env
          volumeMounts: volumeMounts
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: scaleRules
      }
      volumes: volumes
    }
  }
}

output fqdn string = isQueueConsumer ? '' : app.properties.configuration.ingress.fqdn
output id string = app.id
