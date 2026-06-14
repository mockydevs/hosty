
import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { toast } from "sonner";
import { Trash2 } from "lucide-react";

export function ScheduledTasksList({ stackId }: { stackId: number }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");
  const [cron, setCron] = useState("");

  const { data: tasks = [] } = useQuery({
    queryKey: ["stacks", stackId, "tasks"],
    queryFn: async () => {
      // @ts-ignore
      const res = await api.GET(`/api/stacks/${stackId}/scheduled-tasks` as any);
      return res.data || [];
    }
  });

  const addTask = useMutation({
    mutationFn: async () => {
      // @ts-ignore
      const res = await api.POST(`/api/stacks/${stackId}/scheduled-tasks`, { body: { name, command, cron_schedule: cron } });
      if (res.error) throw new Error("Failed to add task");
      return res.data as any;
    },
    onSuccess: () => {
      setName(""); setCommand(""); setCron("");
      queryClient.invalidateQueries({ queryKey: ["stacks", stackId, "tasks"] });
      toast.success("Task added");
    }
  });

  const removeTask = useMutation({
    mutationFn: async (id: number) => {
      // @ts-ignore
      const res = await api.DELETE(`/api/stacks/${stackId}/scheduled-tasks/${id}`);
      if (res.error) throw new Error("Failed to remove task");
      return res.data as any;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["stacks", stackId, "tasks"] });
      toast.success("Task removed");
    }
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Scheduled Tasks</CardTitle>
        <CardDescription>Run cron jobs inside your application container.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Command</TableHead>
              <TableHead>Schedule</TableHead>
              <TableHead className="w-[100px]"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {tasks.map((t: any) => (
              <TableRow key={t.id}>
                <TableCell>{t.name}</TableCell>
                <TableCell className="font-mono text-sm">{t.command}</TableCell>
                <TableCell>{t.cron_schedule}</TableCell>
                <TableCell className="text-right">
                  <Button variant="ghost" size="icon" onClick={() => removeTask.mutate(t.id)}>
                    <Trash2 className="w-4 h-4 text-destructive" />
                  </Button>
                </TableCell>
              </TableRow>
            ))}
            {tasks.length === 0 && (
              <TableRow>
                <TableCell colSpan={4} className="text-center text-muted-foreground">No scheduled tasks configured.</TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>

        <div className="grid gap-4 pt-4 border-t">
          <div className="grid grid-cols-3 gap-4">
            <div className="space-y-2"><Label>Name</Label><Input value={name} onChange={e=>setName(e.target.value)} placeholder="e.g. Clear cache" /></div>
            <div className="space-y-2"><Label>Command</Label><Input value={command} onChange={e=>setCommand(e.target.value)} placeholder="php artisan cache:clear" /></div>
            <div className="space-y-2"><Label>Cron Schedule</Label><Input value={cron} onChange={e=>setCron(e.target.value)} placeholder="0 0 * * *" /></div>
          </div>
          <Button onClick={() => addTask.mutate()} disabled={!name || !command || !cron}>Add Task</Button>
        </div>
      </CardContent>
    </Card>
  );
}
