"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { useMe } from "@/components/shell";
import { Button, Card, Dialog, DialogContent, DialogTrigger, Field, Input, Notice, Spinner } from "@/components/ui";
import { api, describeError } from "@/lib/api";
import { AUTH_MODE, useAuth } from "@/lib/auth";

export default function Settings() {
  const me = useMe();
  const { signOut } = useAuth();
  const [confirm, setConfirm] = useState("");
  const remove = useMutation({ mutationFn: async () => api.DELETE("/v1/me"), onSuccess: async () => { await signOut(); window.location.assign("/sign-in"); } });
  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <h1 className="text-3xl font-bold">Settings</h1>
      <Card className="space-y-3 p-6">
        <h2 className="text-xl font-bold">Account</h2>
        {me.isPending ? <Spinner /> : me.isError ? <Notice tone="danger" title="Couldn't load your account">{describeError(me.error)}</Notice> : (
          <dl className="grid grid-cols-[8rem_1fr] gap-y-2 text-sm"><dt className="text-muted">Email</dt><dd>{me.data.email ?? "Not shared"}</dd><dt className="text-muted">Plan</dt><dd className="capitalize">{me.data.plan}</dd><dt className="text-muted">Sign-in</dt><dd>{AUTH_MODE === "clerk" ? "Managed by Clerk" : "Local development"}</dd></dl>
        )}
        <Button onClick={() => void signOut()}>Sign out</Button>
      </Card>
      <Card className="space-y-3 p-6">
        <h2 className="text-xl font-bold">Your content</h2>
        <p className="text-sm text-muted">Comics are AI-generated. Rights in AI-generated images vary by country, so check the terms before commercial use. Deleted comics are removed from storage within a few days; exports expire automatically.</p>
      </Card>
      <Card className="space-y-3 border-danger p-6">
        <h2 className="text-xl font-bold text-danger">Delete account</h2>
        <p className="text-sm text-muted">Deletes every comic, image and export now, and cancels anything in progress. Unused credits are lost. This can&apos;t be undone.</p>
        <Dialog onOpenChange={() => setConfirm("")}>
          <DialogTrigger asChild><Button variant="danger">Delete my account</Button></DialogTrigger>
          <DialogContent title="Delete your account?" description="Type DELETE to confirm. All of your comics and credits will be removed.">
            <Field label="Confirmation" error={remove.isError ? describeError(remove.error) : null}>{(ids) => <Input {...ids} value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" />}</Field>
            <div className="mt-4 flex justify-end"><Button variant="danger" disabled={confirm !== "DELETE"} busy={remove.isPending} onClick={() => remove.mutate()}>Delete everything</Button></div>
          </DialogContent>
        </Dialog>
      </Card>
    </div>
  );
}
