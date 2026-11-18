/**
 * Sample comics for the public pages.
 *
 * PUBLICATION RULE: a sample is shown publicly only when `approved` is true, which means the owner
 * of the comic has agreed to public display. Until then it renders as a neutral placeholder,
 * except in a local preview build (NEXT_PUBLIC_PREVIEW_UNAPPROVED_SAMPLES=1 together with
 * scripts/preview-samples.sh), where it is labelled "Local preview: not approved for publication"
 * and the page is marked noindex. The image files live in preview-samples/ (not served);
 * public/samples/ is git-ignored, so a production build never contains them.
 *
 * Descriptions are taken from each comic's own Story Bible and pages. The style preset used for
 * these two comics isn't recorded in the exports, so no style is claimed (fill `style` in when it is
 * known).
 */
export type Sample = {
  slug: string;
  title: string;
  logline: string;
  format: string;
  style?: string;
  pages: { src800: string; src1600: string; width: number; height: number; alt: string }[];
  approved: boolean;
  provenance: string;
};

export const SAMPLES: Sample[] = [
  {
    slug: "the-last-train-home",
    title: "The Last Train Home",
    logline: "In a sunlit station café, a girl sketches the boy who always arrives just as his train is leaving.",
    format: "One page · 6 panels",
    pages: [{
      src800: "/samples/the-last-train-home-800.webp", src1600: "/samples/the-last-train-home-1600.webp", width: 1600, height: 2400,
      alt: "A comic page in six panels. A girl sketches in a station café as a boy with a yellow scarf rushes past the window. He leans over her table, apologising for missing his train again. She smiles over a sketchbook full of drawings of him. When the train is delayed he runs for it, and on the platform she calls after him: “Tomorrow… same table?” as he waves from the carriage door.",
    }],
    approved: false,
    provenance: "Made with StoryPanel during testing (November 2026). Permission for public display not yet recorded.",
  },
  {
    slug: "the-tulips-remember",
    title: "The Tulips Remember",
    logline: "An old man returns to a walled garden and finds the tulips in bloom, just as they always were.",
    format: "One page · 5 panels",
    pages: [{
      src800: "/samples/the-tulips-remember-800.webp", src1600: "/samples/the-tulips-remember-1600.webp", width: 1600, height: 2400,
      alt: "A comic page in five panels. An old man in a flat cap opens the gate of a walled garden full of red tulips and walks slowly along the path. In close-up he murmurs: “Oh… you remembered.” He kneels to cup a tulip, then sits on a wooden bench beside a fallen red petal.",
    }],
    approved: false,
    provenance: "Made with StoryPanel during testing (November 2026). Permission for public display not yet recorded.",
  },
];

/** Whether unapproved samples may be shown: local preview builds only. */
export const PREVIEW_UNAPPROVED = process.env.NEXT_PUBLIC_PREVIEW_UNAPPROVED_SAMPLES === "1";

export const visible = (s: Sample) => s.approved || PREVIEW_UNAPPROVED;
export const anyUnapprovedShown = () => PREVIEW_UNAPPROVED && SAMPLES.some((s) => !s.approved);
export const sampleBySlug = (slug: string) => SAMPLES.find((s) => s.slug === slug);
