import {
  IAuthenticateGeneric,
  ICredentialType,
  INodeProperties,
  ICredentialTestRequest,
} from 'n8n-workflow';

export class CogneeApi implements ICredentialType {
  name = 'cogneeApi';
  displayName = 'Cognee API';
  icon = { light: 'file:cognee.svg', dark: 'file:cognee.dark.svg' } as const;
  documentationUrl = 'https://docs.cognee.ai/how-to-guides/cognee-cloud';

  properties: INodeProperties[] = [
    {
      displayName: 'Base URL',
      name: 'baseUrl',
      type: 'string',
      default: '',
      placeholder: 'https://tenant-xxx.aws.cognee.ai',
      description:
        'Copy the Base URL from your Cognee dashboard (API Keys page).',
    },
    {
      displayName: 'API Key',
      name: 'apiKey',
      type: 'string',
      typeOptions: {
        password: true,
      },
      default: '',
      description:
        'Your Cognee API key, sent in the `X-Api-Key` header for authentication.',
    },
  ];

  // Attach the API key to every request made with this credential, including
  // loadOptions lookups that run outside the node's declarative routing.
  authenticate: IAuthenticateGeneric = {
    type: 'generic',
    properties: {
      headers: {
        'X-Api-Key': '={{$credentials.apiKey}}',
      },
    },
  };

  // Test the credential by making a simple API request
  test: ICredentialTestRequest = {
    request: {
      baseURL: '={{$credentials.baseUrl}}',
      url: '/health',
      headers: {
        'X-Api-Key': '={{$credentials.apiKey}}',
      },
    },
  };
}
