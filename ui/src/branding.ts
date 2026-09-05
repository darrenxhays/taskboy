import { createContext, useContext } from "react";

// profile picture urls from /api/me, shared with every avatar badge so pages keep passing only names.
// null until /api/me resolves: the badges show initials in the meantime.
export type Branding = { agentAvatarUrl: string | null; reviewerAvatarUrl: string | null };

export const BrandingContext = createContext<Branding>({ agentAvatarUrl: null, reviewerAvatarUrl: null });

export function useBranding(): Branding {
  return useContext(BrandingContext);
}
