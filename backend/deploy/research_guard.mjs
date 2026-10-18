// Supported OpenCode plugin; no dependencies and no model-side enforcement.
export const StreetStoryResearchGuard = async (ctx, options) => {
  if (!options?.marker || !ctx.directory) {
    throw new Error("research_guard_config_invalid");
  }
  return {
    config: async (cfg) => { cfg.username = options.marker; },
    "tool.execute.before": async ({ tool, sessionID, callID }) => {
      if (tool !== "websearch" || !/^ses[A-Za-z0-9_-]+$/.test(sessionID ?? "") || !callID) {
        throw new Error("research_tool_denied");
      }
    },
  };
};
