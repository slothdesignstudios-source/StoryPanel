"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Badge, Button, Card, EmptyState, Notice, PageHeader, Spinner, cn } from "@/components/ui";
import { api, describeError } from "@/lib/api";
import { pk } from "@/lib/session";

const PLAN_BLURB: Record<string, string> = { free: "Try it out. Exports carry a small StoryPanel mark.", creator: "All style presets, no watermark.", pro: "The most credits each month, no watermark." };

export default function Billing() {
  const q = useQuery({ queryKey: pk("credits"), queryFn: async () => (await api.GET("/v1/credits")).data! });
  const checkout = useMutation({ mutationFn: async (product: "creator" | "pro" | "credit_pack") => (await api.POST("/v1/billing/checkout", { body: { product } })).data!, onSuccess: (r) => window.location.assign(r.url) });
  const portal = useMutation({ mutationFn: async () => (await api.POST("/v1/billing/portal")).data!, onSuccess: (r) => window.location.assign(r.url) });
  if (q.isPending) return <Spinner label="Loading credits" />;
  if (q.isError) return <Notice tone="danger" title="We couldn't load your credits" action={<Button onClick={() => void q.refetch()}>Try again</Button>}>{describeError(q.error)}</Notice>;
  const c = q.data;
  const error = checkout.error ?? portal.error;
  return (
    <div className="space-y-8">
      <PageHeader title="Credits" lead="Credits are held when work starts and only charged for what is delivered." actions={<Button asChild variant="ghost"><Link href="/pricing">How pricing works</Link></Button>} />
      <div className="grid gap-4 sm:grid-cols-3">
        <Card className="border-accent/25 bg-accent-soft p-5"><p className="text-sm font-semibold text-accent">Available to spend</p><p className="mt-1 font-display text-4xl font-semibold tabular-nums">{c.available}</p></Card>
        <Card className="p-5"><p className="text-sm font-semibold text-muted">Held for comics in progress</p><p className="mt-1 font-display text-4xl font-semibold tabular-nums">{c.reserved}</p><p className="mt-1 text-xs text-muted">Charged only if the work is delivered; otherwise released.</p></Card>
        <Card className="p-5"><p className="text-sm font-semibold text-muted">Plan</p><p className="mt-1 font-display text-4xl font-semibold capitalize">{c.plan}</p>{c.subscription_status && <Badge className="mt-1">{c.subscription_status.replace("_", " ")}</Badge>}</Card>
      </div>
      <section aria-labelledby="prices" className="space-y-3">
        <h2 id="prices" className="text-xl">What things cost</h2>
        <dl className="grid gap-2 text-sm sm:grid-cols-3 lg:grid-cols-6">
          {([["Cast and sheets", c.prices.cast], ["Script", c.prices.script], ["Each panel", c.prices.panel], ["Regenerate", c.prices.regenerate], ["Fix by instruction", c.prices.edit], ["Character revision", c.prices.character_revision]] as const).map(([k, v]) => (
            <div key={k} className="rounded-md border border-line bg-card p-3"><dt className="text-muted">{k}</dt><dd className="font-semibold">{v} credit{v === 1 ? "" : "s"}</dd></div>
          ))}
        </dl>
        <ul className="list-inside list-disc space-y-1 text-sm text-muted">
          <li>Credits are held when work starts. Only delivered work is charged; whatever isn&apos;t used is released.</li>
          <li>Automatic quality retries are free. Panels that fail or are blocked by the safety check aren&apos;t charged.</li>
          <li>A delivered panel is charged even when our checks flag it as needing attention.</li>
          <li>A regenerate or fix that produces an image is charged, even if you started another change before it finished.</li>
        </ul>
      </section>
      <section aria-labelledby="plans" className="space-y-3">
        <h2 id="plans" className="text-xl">Plans and top-ups</h2>
        {!c.billing_enabled && <Notice title="Purchases aren't switched on in this environment">Payments need Stripe to be configured. Your existing credits work as normal.</Notice>}
        {error && <Notice tone="danger" title="We couldn't open checkout">{describeError(error)}</Notice>}
        <div className="grid gap-4 md:grid-cols-4">
          {(["free", "creator", "pro"] as const).map((plan) => (
            <Card key={plan} className={cn("flex flex-col gap-3 p-5", c.plan === plan && "bg-accent-soft")}>
              <div className="flex items-center justify-between"><h3 className="text-lg font-semibold capitalize">{plan}</h3>{c.plan === plan && <Badge tone="working">Current</Badge>}</div>
              <p className="font-display text-2xl font-semibold">{c.plans[plan] ?? 0} <span className="text-sm font-normal text-muted">credits / month</span></p>
              <p className="flex-1 text-sm text-muted">{PLAN_BLURB[plan]}</p>
              {plan !== "free" && c.plan !== plan && <Button variant="primary" disabled={!c.billing_enabled} busy={checkout.isPending && checkout.variables === plan} onClick={() => checkout.mutate(plan)}>Choose {plan}</Button>}
              {plan !== "free" && c.plan === plan && <Button disabled={!c.billing_enabled} busy={portal.isPending} onClick={() => portal.mutate()}>Manage subscription</Button>}
            </Card>
          ))}
          <Card className="flex flex-col gap-3 p-5">
            <h3 className="text-lg font-semibold">Credit pack</h3>
            <p className="font-display text-2xl font-semibold">{c.prices.credit_pack} <span className="text-sm font-normal text-muted">credits, once</span></p>
            <p className="flex-1 text-sm text-muted">A one-off top-up on any plan.</p>
            <Button disabled={!c.billing_enabled} busy={checkout.isPending && checkout.variables === "credit_pack"} onClick={() => checkout.mutate("credit_pack")}>Buy a pack</Button>
          </Card>
        </div>
      </section>
      <section aria-labelledby="holds" className="space-y-3">
        <h2 id="holds" className="text-xl">Held for work in progress</h2>
        {c.holds.length === 0 ? <p className="text-sm text-muted">Nothing is held right now.</p> : (
          <ul className="grid gap-3 sm:grid-cols-2">
            {c.holds.map((h) => (
              <li key={h.id} className="rounded-md border border-line bg-card p-3 text-sm">
                <p className="font-semibold">{h.label}{h.project_title ? <span className="font-normal text-muted"> · {h.project_title}</span> : null}</p>
                <p><span className="font-semibold tabular-nums">{h.remaining}</span> held{h.charged ? <span className="text-muted"> ({h.charged} of {h.held} already charged)</span> : null}</p>
                <p className="text-xs text-muted">Since {new Date(h.opened_at).toLocaleString()}{h.history === "unavailable" ? " · made before hold history was recorded" : ""}</p>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-labelledby="history" className="space-y-3">
        <h2 id="history" className="text-xl">Activity</h2>
        {c.holds_without_history > 0 && (
          <Notice title="Some older holds aren't listed">Holds and releases{c.hold_history_since ? ` before ${new Date(c.hold_history_since).toLocaleDateString()}` : ""} weren&apos;t recorded, so they don&apos;t appear below. Charges, grants and the totals above are exact.</Notice>
        )}
        {c.activity.length === 0 ? <EmptyState title="No activity yet">Holds, charges and top-ups will be listed here.</EmptyState> : (
          <div className="overflow-x-auto rounded-lg border border-line bg-card">
            <table className="w-full min-w-[40rem] text-left text-sm">
              <caption className="sr-only">Credit activity, newest first. A charge taken from a hold doesn&apos;t change what is available, because the hold already set it aside.</caption>
              <thead className="border-b border-line bg-paper"><tr><th scope="col" className="p-3">When</th><th scope="col" className="p-3">What</th><th scope="col" className="p-3 text-right">Available</th><th scope="col" className="p-3 text-right">Balance</th></tr></thead>
              <tbody>
                {c.activity.map((a, n) => (
                  <tr key={`${a.at}-${n}`} className="border-b border-line last:border-0">
                    <td className="p-3 text-muted">{new Date(a.at).toLocaleString()}</td>
                    <td className="p-3">{a.reason}{a.project_title ? <span className="text-muted"> · {a.project_title}</span> : null}{a.type === "charge" && a.held_change !== 0 ? <span className="block text-xs text-muted">Charged from a hold</span> : null}</td>
                    <td className={cn("p-3 text-right font-semibold tabular-nums", a.available_change > 0 ? "text-ok" : "text-ink")}>{a.available_change > 0 ? `+${a.available_change}` : a.available_change === 0 ? "0" : a.available_change}</td>
                    <td className="p-3 text-right tabular-nums">{a.balance_change !== 0 ? <>{a.balance_change > 0 ? `+${a.balance_change}` : a.balance_change}<span className="text-muted"> → {a.balance_after}</span></> : <span className="text-muted">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
