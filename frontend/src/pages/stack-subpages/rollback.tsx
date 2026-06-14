import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { RotateCcw } from "lucide-react";

export function RollbackList() {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Rollback</CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Deployment ID</TableHead>
              <TableHead>Date</TableHead>
              <TableHead className="w-24" />
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableRow>
              <TableCell className="font-mono text-xs">dep_xyz12345</TableCell>
              <TableCell className="text-sm text-muted-foreground">2 days ago</TableCell>
              <TableCell>
                <Button variant="outline" size="sm"><RotateCcw className="h-3 w-3 mr-2" /> Rollback</Button>
              </TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
