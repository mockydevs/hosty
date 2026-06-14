import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";

export function DeploymentsTab() {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Deployment History</CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Status</TableHead>
              <TableHead>Commit / Version</TableHead>
              <TableHead>Triggered By</TableHead>
              <TableHead>Date</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableRow>
              <TableCell><Badge>Success</Badge></TableCell>
              <TableCell className="font-mono text-xs">initial</TableCell>
              <TableCell>Admin</TableCell>
              <TableCell className="text-sm text-muted-foreground">Just now</TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
