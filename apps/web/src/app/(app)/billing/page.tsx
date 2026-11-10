"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { Badge, Button, Card, EmptyState, Notice, Spinner, cn } from "@/components/ui";
import { api, describeError } from "@/lib/api";

const PLAN_BLURB: Record<string, string> = { free: "Try it out. Exports carry a small StoryPanel mark.", creator: "All style presets, no watermark.", pro: "The most credits each month, no watermark." };

export default function Billing() {
  const q = useQuery({ queryKey: ["credits"], queryFn: async () => (await api.GET("/v1/credits")).data! });
  const checkout = useMutation({ mutationFn: async (product: "creator" | "pro" | "credit_pack") => (await api.POST("/v1/billing/checkout", { body: { product } })).data!, onSuccess: (r) => window.location.assign(r.url) });
  const portal = useMutation({ mutationFn: async () => (await api.POST("/v1/billing/portal")).data!, onSuccess: (r) => window.location.assign(r.url) });
  if (q.isPending) return <Spinner label="Loading credits" />;
  if (q.isError) return <Notice tone="danger" title="We couldn't load your credits" action={<Button onClick={() => void q.refetch()}>Try again</Button>}>{describeError(q.error)}</Notice>;
  const c = q.data;
  const error = checkout.error ?? portal.error;
  return (
    <div className="space-y-8">
      <div><h1 className="text-3xl font-bold">Credits</h1><p className="text-sm text-muted">Credits are held when work starts and only charged for what is delivered.</p></div>
      <div className="grid gap-4 sm:grid-cols-3">
        <Card className="bg-sun p-5"><p className="text-sm font-semibold">Available</p><p className="font-display text-4xl font-bold">{c.available}</p></Card>
        <Card className="p-5"><p className="text-sm font-semibold">Held for comics in progress</p><p className="font-display text-4xl font-bold">{c.reserved}</p></Card>
        <Card className="p-5"><p className="text-sm font-semibold">Plan</p><p className="font-display text-4xl font-bold capitalize">{c.plan}</p>{c.subscription_status && <Badge className="mt-1">{c.subscription_status.replace("_", " ")}</Badge>}</Card>
      </div>
      <section aria-labelledby="prices" className="space-y-3">
        <h2 id="prices" className="text-xl font-bold">What things cost</h2>
        <dl className="grid gap-2 text-sm sm:grid-cols-3 lg:grid-cols-6">
          {([["Cast and sheets", c.prices.cast], ["Script", c.prices.script], ["Each panel", c.prices.panel], ["Regenerate", c.prices.regenerate], ["Fix by instruction", c.prices.edit], ["Character revision", c.prices.character_revision]] as const).map(([k, v]) => (
            <div key={k} className="rounded-md border-2 border-line bg-card p-3"><dt className="text-muted">{k}</dt><dd className="font-bold">{v} credit{v === 1 ? "" : "s"}</dd></div>
          ))}
        </dl>
        <p className="text-sm text-muted">Automatic quality retries, failed panels and safety-blocked panels are never charged.</p>
      </section>
      <section aria-labelledby="plans" className="space-y-3">
        <h2 id="plans" className="text-xl font-bold">Plans and top-ups</h2>
        {!c.billing_enabled && <Notice title="Purchases aren't switched on in this environment">Payments need Stripe to be configured. Your existing credits work as normal.</Notice>}
        {error && <Notice tone="danger" title="We couldn't open checkout">{describeError(error)}</Notice>}
        <div className="grid gap-4 md:grid-cols-4">
          {(["free", "creator", "pro"] as const).map((plan) => (
            <Card key={plan} className={cn("flex flex-col gap-3 p-5", c.plan === plan && "bg-teal/5")}>
              <div className="flex items-center justify-between"><h3 className="text-lg font-bold capitalize">{plan}</h3>{c.plan === plan && <Badge tone="working">Current</Badge>}</div>
              <p className="font-display text-2xl font-bold">{c.plans[plan] ?? 0} <span className="text-sm font-normal text-muted">credits / month</span></p>
              <p className="flex-1 text-sm text-muted">{PLAN_BLURB[plan]}</p>
              {plan !== "free" && c.plan !== plan && <Button variant="primary" disabled={!c.billing_enabled} busy={checkout.isPending && checkout.variables === plan} onClick={() => checkout.mutate(plan)}>Choose {plan}</Button>}
              {plan !== "free" && c.plan === plan && <Button disabled={!c.billing_enabled} busy={portal.isPending} onClick={() => portal.mutate()}>Manage subscription</Button>}
            </Card>
          ))}
          <Card className="flex flex-col gap-3 p-5">
            <h3 className="text-lg font-bold">Credit pack</h3>
            <p className="font-display text-2xl font-bold">{c.prices.credit_pack} <span className="text-sm font-normal text-muted">credits, once</span></p>
            <p className="flex-1 text-sm text-muted">A one-off top-up on any plan.</p>
            <Button disabled={!c.billing_enabled} busy={checkout.isPending && checkout.variables === "credit_pack"} onClick={() => checkout.mutate("credit_pack")}>Buy a pack</Button>
          </Card>
        </div>
      </section>
      <section aria-labelledby="history" className="space-y-3">
        <h2 id="history" className="text-xl font-bold">History</h2>
        {c.history.length === 0 ? <EmptyState title="No activity yet">Charges and top-ups will be listed here.</EmptyState> : (
          <div className="overflow-x-auto rounded-lg border-2 border-ink bg-card">
            <table className="w-full min-w-[32rem] text-left text-sm">
              <caption className="sr-only">Credit history, newest first</caption>
              <thead className="border-b-2 border-ink bg-paper"><tr><th scope="col" className="p-3">When</th><th scope="col" className="p-3">What</th><th scope="col" className="p-3 text-right">Credits</th><th scope="col" className="p-3 text-right">Balance</th></tr></thead>
              <tbody>
                {c.history.map((h) => (
                  <tr key={h.id} className="border-b border-line last:border-0">
                    <td className="p-3 text-muted">{new Date(h.created_at).toLocaleString()}</td><td className="p-3">{h.reason}</td>
                    <td className={cn("p-3 text-right font-semibold tabular-nums", h.delta > 0 ? "text-ok" : "text-ink")}>{h.delta > 0 ? `+${h.delta}` : h.delta}</td>
                    <td className="p-3 text-right tabular-nums">{h.balance_after}</td>
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
