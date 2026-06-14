
import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { toast } from "sonner";

export function ResourceLimitsConfig({ stackId, inputs }: { stackId: number, inputs: any }) {
  const queryClient = useQueryClient();
  const [cpu, setCpu] = useState(inputs.cpu_limit || "");
  const [mem, setMem] = useState(inputs.memory_limit || "");

  const save = useMutation({
    mutationFn: async () => {
      // @ts-ignore
      const res = await api.PATCH(`/api/stacks/${stackId}/config`, { body: { cpu_limit: Number(cpu) || null, memory_limit: Number(mem) || null } });
      if (res.error) throw new Error("Failed to save limits");
      return res.data;
    },
    onSuccess: () => {
      toast.success("Resource limits saved");
      queryClient.invalidateQueries({ queryKey: ["stacks", stackId] });
    }
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Resource Limits</CardTitle>
        <CardDescription>Limit CPU and memory usage for this application.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-2">
          <Label>CPUs (percentage, e.g. 100 = 1 CPU)</Label>
          <Input type="number" placeholder="Leave blank for unlimited" value={cpu} onChange={e=>setCpu(e.target.value)} />
        </div>
        <div className="grid gap-2">
          <Label>Memory (MB)</Label>
          <Input type="number" placeholder="Leave blank for unlimited" value={mem} onChange={e=>setMem(e.target.value)} />
        </div>
        <Button onClick={() => save.mutate()}>Save Limits</Button>
      </CardContent>
    </Card>
  );
}
