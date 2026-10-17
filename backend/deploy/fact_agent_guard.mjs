// Dedicated fact-agent capsule. Existing source-reader guard remains unchanged.
import { lookup } from 'node:dns/promises';
import { isIP } from 'node:net';

const publicAddress = (address) => {
  if (isIP(address) === 4) {
    const [a, b] = address.split('.').map(Number);
    return !(a === 0 || a === 10 || a === 127 || a >= 224 ||
      (a === 100 && b >= 64 && b <= 127) || (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) || (a === 192 && (b === 0 || b === 168)) ||
      (a === 198 && (b === 18 || b === 19 || b === 51)) || (a === 203 && b === 0));
  }
  return isIP(address) === 6 && /^[23]/i.test(address) && !/^2001:db8:/i.test(address);
};

export const StreetStoryFactAgentGuard = async (ctx, options) => {
  if (!options?.marker || !ctx.directory) throw new Error('research_guard_configuration_required');
  return {
    config: async (config) => { config.username = options.marker; },
    'tool.execute.before': async ({ tool, sessionID, callID }, { args }) => {
      if (!['websearch', 'webfetch'].includes(tool) || !/^ses[A-Za-z0-9_-]+$/.test(sessionID) || !callID)
        throw new Error('research_tool_denied');
      if (tool === 'webfetch') {
        let url;
        try { url = new URL(args.url); } catch { throw new Error('research_public_url_required'); }
        if (url.protocol !== 'https:' || url.username || url.password || (url.port && url.port !== '443'))
          throw new Error('research_public_url_required');
        const host = url.hostname.replace(/^\[|\]$/g, '');
        const records = isIP(host) ? [{ address: host }] : await lookup(host, { all: true });
        if (!records.length || records.some(({ address }) => !publicAddress(address)))
          throw new Error('research_public_url_required');
      }
    },
  };
};
