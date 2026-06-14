
import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { toast } from "sonner";
import { Webhook, Unlink } from "lucide-react";

export function GitSourceSettings({ stackId, inputs }: { stackId: number, inputs: any }) {
  const queryClient = useQueryClient();
  const [repo, setRepo] = useState(inputs.repo || "");
  const [branch, setBranch] = useState(inputs.branch || "");

  const { data: sources = [] } = useQuery({
    queryKey: ["sources"],
    queryFn: async () => {
      // @ts-ignore
      const res = await api.GET("/api/sources" as any);
      return (res.data as any[]) || [];
    },
  });

  const linkedSourceId = inputs.source_id ? Number(inputs.source_id) : null;
  const linkedSource = sources.find((s: any) => s.id === linkedSourceId);

  const save = useMutation({
    mutationFn: async () => {
      // @ts-ignore
      const res = await api.PATCH(`/api/stacks/${stackId}/config`, { body: { repo, branch } });
      if (res.error) throw new Error("Failed to save");
      return res.data as any;
    },
    onSuccess: () => {
      toast.success("Git settings saved");
      queryClient.invalidateQueries({ queryKey: ["stacks", stackId] });
    },
  });

  const linkSource = useMutation({
    mutationFn: async (sourceId: number) => {
      // @ts-ignore
      const res = await api.PATCH(`/api/stacks/${stackId}/config`, { body: { source_id: sourceId } });
      if (res.error) throw new Error("Failed to link source");
      return res.data as any;
    },
    onSuccess: () => {
      toast.success("GitHub App linked — pushes to this repo/branch will trigger redeployment");
      queryClient.invalidateQueries({ queryKey: ["stacks", stackId] });
    },
    onError: () => toast.error("Failed to link GitHub App"),
  });

  const unlinkSource = useMutation({
    mutationFn: async () => {
      // @ts-ignore — send source_id=0 to clear
      const res = await api.PATCH(`/api/stacks/${stackId}/config`, { body: { source_id: 0 } });
      if (res.error) throw new Error("Failed to unlink");
      return res.data as any;
    },
    onSuccess: () => {
      toast.success("GitHub App unlinked");
      queryClient.invalidateQueries({ queryKey: ["stacks", stackId] });
    },
    onError: () => toast.error("Failed to unlink GitHub App"),
  });

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Git Source</CardTitle>
          <CardDescription>Where your code comes from.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-2">
            <Label>Repository URL</Label>
            <Input value={repo} onChange={e => setRepo(e.target.value)} />
          </div>
          <div className="grid gap-2">
            <Label>Branch</Label>
            <Input value={branch} onChange={e => setBranch(e.target.value)} />
          </div>
          <Button onClick={() => save.mutate()} disabled={save.isPending}>Save Git Settings</Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Webhook className="w-4 h-4" />
            Auto-Deploy via GitHub App
          </CardTitle>
          <CardDescription>
            Link a GitHub App source to automatically redeploy when you push to this branch.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {linkedSource ? (
            <div className="flex items-center justify-between rounded-md border px-4 py-3">
              <div className="flex items-center gap-3">
                <Badge variant="outline" className="text-green-600 border-green-500">Connected</Badge>
                <span className="text-sm font-medium">{linkedSource.name}</span>
                <span className="text-xs text-muted-foreground">{linkedSource.provider}</span>
              </div>
              <Button
                variant="ghost"
                size="sm"
                className="text-destructive"
                onClick={() => unlinkSource.mutate()}
                disabled={unlinkSource.isPending}
              >
                <Unlink className="w-3.5 h-3.5 mr-1.5" />
                Unlink
              </Button>
            </div>
          ) : (
            <div className="space-y-3">
              {sources.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No GitHub Apps connected yet.{" "}
                  <a href="/sources" className="underline underline-offset-2">Add a Git source</a>{" "}
                  to enable automatic redeployment on push.
                </p>
              ) : (
                <>
                  <Label>Select GitHub App</Label>
                  <div className="flex gap-2">
                    <Select
                      onValueChange={(val) => linkSource.mutate(Number(val))}
                      disabled={linkSource.isPending}
                    >
                      <SelectTrigger className="flex-1">
                        <SelectValue placeholder="Choose a GitHub App…" />
                      </SelectTrigger>
                      <SelectContent>
                        {sources.map((s: any) => (
                          <SelectItem key={s.id} value={String(s.id)}>
                            {s.name}
                            {!s.installation_id && (
                              <span className="ml-2 text-xs text-amber-500">(not installed)</span>
                            )}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    The GitHub App must have access to the repository above.
                  </p>
                </>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
