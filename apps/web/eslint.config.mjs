import next from "eslint-config-next";

const config = [
  ...next,
  {
    rules: {
      // Hydrating from sessionStorage, and resetting a local draft when the server version
      // changes, are deliberate one-shot state syncs; the alternatives are worse here.
      "react-hooks/set-state-in-effect": "off",
      "import/no-anonymous-default-export": "off",
      "@next/next/no-html-link-for-pages": "off",
    },
  },
  { ignores: [".next/**", "node_modules/**", "playwright-report/**", "test-results/**", "next-env.d.ts"] },
];
export default config;
