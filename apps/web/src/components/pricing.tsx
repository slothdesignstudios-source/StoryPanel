import { examples } from "@/lib/public-meta";
import type { Meta } from "@/lib/types";
import { Notice } from "./ui";

const ROWS: [key: string, what: string, when: string][] = [
  ["cast", "Cast and reference sheets", "Charged once the cast is designed"],
  ["script", "Script", "Charged once the script is written"],
  ["panel", "Each drawn panel", "Charged when the panel is delivered"],
  ["regenerate", "Redraw a panel", "Charged only if a new image is delivered"],
  ["edit", "Fix a panel by instruction", "Charged only if a new image is delivered"],
  ["character_revision", "Revise a character sheet", "Charged only if a new sheet is delivered"],
];

const credits = (n: number) => `${n} credit${n === 1 ? "" : "s"}`;

/** What things cost, from the live product configuration (`/v1/meta`). */
export function CreditPrices({ meta }: { meta: Meta | null }) {
  if (!meta) {
    return <Notice title="Current prices couldn't be loaded">The pricing service didn&apos;t answer just now. Prices are shown in your account before anything is charged.</Notice>;
  }
  const p = meta.credit_prices;
  const ex = examples(p);
  return (
    <div className="grid gap-8 lg:grid-cols-[3fr_2fr]">
      <div className="overflow-hidden rounded-xl border border-line bg-card shadow-raised">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Credit cost of each step</caption>
          <thead className="border-b border-line bg-paper text-muted">
            <tr><th scope="col" className="px-4 py-3 font-semibold">Step</th><th scope="col" className="px-4 py-3 text-right font-semibold">Credits</th><th scope="col" className="hidden px-4 py-3 font-semibold sm:table-cell">When it&apos;s charged</th></tr>
          </thead>
          <tbody>
            {ROWS.map(([k, what, when]) => (
              <tr key={k} className="border-b border-line last:border-0">
                <th scope="row" className="px-4 py-3 font-medium">{what}<span className="block text-xs font-normal text-muted sm:hidden">{when}</span></th>
                <td className="px-4 py-3 text-right font-semibold tabular-nums">{p[k] ?? "—"}</td>
                <td className="hidden px-4 py-3 text-muted sm:table-cell">{when}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="space-y-4">
        <div className="rounded-xl border border-line bg-card p-5 shadow-raised">
          <h3 className="text-lg">Worked examples</h3>
          <dl className="mt-3 space-y-2 text-sm">
            <div className="flex justify-between gap-4"><dt className="text-muted">A 4-panel strip</dt><dd className="font-semibold tabular-nums">{credits(ex.strip4)}</dd></div>
            <div className="flex justify-between gap-4"><dt className="text-muted">One page of 6 panels</dt><dd className="font-semibold tabular-nums">{credits(ex.page6)}</dd></div>
          </dl>
          <p className="mt-3 text-xs text-muted">Cast ({p.cast}) + script ({p.script}) + {p.panel} per panel. Pages use 4–6 panels; redraws and fixes are extra. Your account shows the exact amount before each step.</p>
        </div>
        <div className="rounded-xl border border-line bg-card p-5 shadow-raised">
          <h3 className="text-lg">Top-ups</h3>
          <p className="mt-2 text-sm text-muted">A credit pack adds {credits(p.credit_pack ?? 0)}. Plans with a monthly credit allowance are listed in your account; the price is shown at checkout.</p>
        </div>
      </div>
    </div>
  );
}

export function HoldsExplained() {
  return (
    <ol className="grid gap-4 sm:grid-cols-3">
      {[
        ["Held", "When a step starts, its credits are set aside. They still belong to you."],
        ["Charged", "Credits are charged only for work that's delivered: a cast, a script, a drawn panel."],
        ["Released", "Anything not used comes back: a failed panel, one blocked by the safety check, or a cancelled comic."],
      ].map(([t, d], i) => (
        <li key={t} className="rounded-xl border border-line bg-card p-5 shadow-raised">
          <p className="text-xs font-semibold text-muted">Step {i + 1}</p>
          <h3 className="mt-1 text-lg">{t}</h3>
          <p className="mt-2 text-sm text-muted">{d}</p>
        </li>
      ))}
    </ol>
  );
}
