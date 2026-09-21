// Azure Service Bus Namespace + Queue
param namespace string
param location string

@allowed(['Basic', 'Standard', 'Premium'])
param tier string = 'Standard'

resource sb 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: namespace
  location: location
  sku: {
    name: tier
    tier: tier
  }
  properties: {
    minimumTlsVersion: '1.2'
  }
}

resource queue 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  name: 'control-plane-queue'
  parent: sb
  properties: {
    defaultMessageTimeToLive: 'PT1H'         // 1 hour TTL
    maxDeliveryCount: 5                       // dead-letter after 5 retries
    lockDuration: 'PT5M'                      // 5 min lock (renewed by service)
    enablePartitioning: false
  }
}

// Root Manage Shared Access Policy (for connection strings)
resource rootPolicy 'Microsoft.ServiceBus/namespaces/authorizationRules@2022-10-01-preview' existing = {
  name: 'RootManageSharedAccessKey'
  parent: sb
}

output connectionString string = rootPolicy.listKeys().primaryConnectionString
output namespaceId string = sb.id
output queueName string = queue.name
