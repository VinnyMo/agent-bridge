// Install as an Auth0 Login/Post Login Action and add it to the Login flow.
// Values below are Auth0 Action secrets, never committed account identifiers.
exports.onExecutePostLogin = async (event, api) => {
  if (event.resource_server?.identifier !== 'https://agent.vincentmossman.com/mcp-v2') return;
  const owner = event.secrets.AGENT_OWNER_SUBJECT;
  const client = event.secrets.AGENT_CHATGPT_CLIENT_ID;
  if (!owner || !client || event.user.user_id !== owner || event.client.client_id !== client) {
    api.access.deny('This connection is not authorized.');
  }
};
