
import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { toast } from "sonner";

export function GitSourceSettings({ stackId, inputs }: { stackId: number, inputs: any }) {
  const queryClient = useQueryClient();
  const [repo, setRepo] = useState(inputs.repo || "");
  const [branch, setBranch] = useState(inputs.branch || "");

  const save = useMutation({
    mutationFn: async () => {
      // @ts-ignore
      const res = await api.PATCH(`/api/stacks/${stackId}/config`, { body: { repo, branch } });
      if (res.error) throw new Error("Failed to save");
      return res.data;
    },
    onSuccess: () => {
      toast.success("Git settings saved");
      queryClient.invalidateQueries({ queryKey: ["stacks", stackId] });
    }
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Git Source</CardTitle>
        <CardDescription>Where your code comes from.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-2">
          <Label>Repository URL</Label>
          <Input value={repo} onChange={e=>setRepo(e.target.value)} />
        </div>
        <div className="grid gap-2">
          <Label>Branch</Label>
          <Input value={branch} onChange={e=>setBranch(e.target.value)} />
        </div>
        <Button onClick={() => save.mutate()}>Save Git Settings</Button>
      </CardContent>
    </Card>
  );
}
