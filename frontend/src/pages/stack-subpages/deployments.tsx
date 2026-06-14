
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api/client";

export function DeploymentsTab({ stackId }: { stackId: number }) {
  const { data: deps = [] } = useQuery({
    queryKey: ["stacks", stackId, "deployments"],
    queryFn: async () => {
      // @ts-ignore
      const res = await api.GET(`/api/stacks/${stackId}/deployments`);
      return res.data || [];
    }
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Deployment History</CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Date</TableHead>
              <TableHead>Commit</TableHead>
              <TableHead>Message</TableHead>
              <TableHead>Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {deps.map((d: any) => (
              <TableRow key={d.id}>
                <TableCell>{new Date(d.created_at).toLocaleString()}</TableCell>
                <TableCell className="font-mono">{d.commit_sha.substring(0,7)}</TableCell>
                <TableCell>{d.message || "Manual deployment"}</TableCell>
                <TableCell><Badge variant={d.status === "success" ? "default" : "secondary"}>{d.status}</Badge></TableCell>
              </TableRow>
            ))}
            {deps.length === 0 && (
              <TableRow><TableCell colSpan={4} className="text-center text-muted-foreground">No deployments found.</TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
