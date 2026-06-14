
import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { toast } from "sonner";

export function AdvancedSettings({ stackId, inputs }: { stackId: number, inputs: any }) {
  const queryClient = useQueryClient();
  const [autoDeploy, setAutoDeploy] = useState(inputs.auto_deploy || false);
  const [forceRebuild, setForceRebuild] = useState(inputs.force_rebuild || false);

  const save = useMutation({
    mutationFn: async () => {
      // @ts-ignore
      const res = await api.PATCH(`/api/stacks/${stackId}/config`, { body: { auto_deploy: autoDeploy, force_rebuild: forceRebuild } });
      if (res.error) throw new Error("Failed to save");
      return res.data;
    },
    onSuccess: () => {
      toast.success("Settings saved");
      queryClient.invalidateQueries({ queryKey: ["stacks", stackId] });
    }
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Advanced Settings</CardTitle>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="flex items-center justify-between">
          <div className="space-y-0.5">
            <Label>Auto Deploy</Label>
            <p className="text-sm text-muted-foreground">Automatically deploy when new commits are pushed.</p>
          </div>
          <input type="checkbox" checked={autoDeploy} onChange={(e) => setAutoDeploy(e.target.checked)} />
        </div>
        <div className="flex items-center justify-between">
          <div className="space-y-0.5">
            <Label>Force Rebuild</Label>
            <p className="text-sm text-muted-foreground">Ignore cache and rebuild from scratch on next deployment.</p>
          </div>
          <input type="checkbox" checked={forceRebuild} onChange={(e) => setForceRebuild(e.target.checked)} />
        </div>
        <Button onClick={() => save.mutate()}>Save Advanced Settings</Button>
      </CardContent>
    </Card>
  );
}
