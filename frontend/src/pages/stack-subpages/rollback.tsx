
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { RotateCcw } from "lucide-react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { toast } from "sonner";

export function RollbackList({ stackId }: { stackId: number }) {
  const { data: deps = [] } = useQuery({
    queryKey: ["stacks", stackId, "deployments"],
    queryFn: async () => {
      // @ts-ignore
      const res = await api.GET(`/api/stacks/${stackId}/deployments`);
      return res.data || [];
    }
  });

  const rollback = useMutation({
    mutationFn: async (deploymentId: number) => {
      // @ts-ignore
      const res = await api.POST(`/api/stacks/${stackId}/deployments/${deploymentId}/rollback`);
      if (res.error) throw new Error("Failed to rollback");
      return res.data;
    },
    onSuccess: () => {
      toast.success("Rollback initiated");
    }
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base text-destructive flex items-center gap-2">
          <RotateCcw className="w-5 h-5" /> Rollback
        </CardTitle>
        <CardDescription>Revert your application to a previous successful deployment.</CardDescription>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Date</TableHead>
              <TableHead>Commit</TableHead>
              <TableHead className="w-[100px]"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {deps.map((d: any) => (
              <TableRow key={d.id}>
                <TableCell>{new Date(d.created_at).toLocaleString()}</TableCell>
                <TableCell className="font-mono">{d.commit_sha.substring(0,7)}</TableCell>
                <TableCell className="text-right">
                  <Button variant="outline" size="sm" onClick={() => rollback.mutate(d.id)}>Rollback</Button>
                </TableCell>
              </TableRow>
            ))}
            {deps.length === 0 && (
              <TableRow><TableCell colSpan={3} className="text-center text-muted-foreground">No deployment history available.</TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
