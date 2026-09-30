{ ExportProtelNetlist.pas
  ---------------------------------------------------------------------------
  Generate a Protel-format (.NET) netlist for the currently focused project
  in Altium Designer — version-independent: instead of the GenerateReport
  process (whose netlist-format Index mapping shifts between AD versions),
  this script compiles the project and walks the connectivity data via the
  Workspace Manager interfaces, writing the Protel records itself.

  Usage / 用法:
    1. Open your PCB project (.PrjPcb) in Altium Designer; click one of its
       schematics so it is the focused project. 打开工程并确保它是焦点工程。
    2. DXP -> Run Script... -> this file -> run ExportProtelNetlist.
       DXP 菜单 -> Run Script，选择本文件，运行 ExportProtelNetlist。
    3. The .NET file is written next to the project; a popup shows the path.
       生成完成后弹出完整路径。

  Command line (AD18+, still opens the AD UI — not headless):
    "C:\Program Files\Altium\AD24\X2.EXE" -RScriptingSystem:RunScript(ProjectName="<this file>"|ProcName="ExportProtelNetlist")

  Note: the ERC messages that appear in the Messages panel during DM_Compile
  (unmatched sheet entries, floating labels, ...) are pre-existing design
  findings, not script errors — the netlist is still written.
  编译期间 Messages 面板里的 ERC 信息是原理图本身的检查结果，与脚本无关。
  --------------------------------------------------------------------------- }

Procedure ExportProtelNetlist;
Var
    WS      : IWorkSpace;
    Prj     : IProject;
    Doc     : IDocument;
    Comp    : IComponent;
    Net     : INet;
    Pin     : IPin;
    I, J, K : Integer;
    Lines     : TStringList;
    OutDir     : String;
    OutPath : String;
    CompCnt : Integer;
    NetCnt  : Integer;
Begin
    WS := GetWorkSpace;
    If WS = Nil Then Exit;
    Prj := WS.DM_FocusedProject;
    If Prj = Nil Then
    Begin
        ShowError('No focused project - open a .PrjPcb and click one of its schematic documents first.');
        Exit;
    End;
    Prj.DM_Compile;

    Lines := TStringList.Create;
    CompCnt := 0;
    NetCnt := 0;
    Try
        { Component records: [ designator / footprint / comment ] }
        For I := 0 To Prj.DM_PhysicalDocumentCount - 1 Do
        Begin
            Doc := Prj.DM_PhysicalDocuments(I);
            For J := 0 To Doc.DM_ComponentCount - 1 Do
            Begin
                Comp := Doc.DM_Components(J);
                Lines.Add('[');
                Lines.Add(Comp.DM_PhysicalDesignator);
                Lines.Add(Comp.DM_Footprint);
                Lines.Add(Comp.DM_Comment);
                Lines.Add('');
                Lines.Add('');
                Lines.Add('');
                Lines.Add(']');
                Inc(CompCnt);
            End;
        End;

        { Net records: ( name / REF-PIN ... ) }
        For I := 0 To Prj.DM_PhysicalDocumentCount - 1 Do
        Begin
            Doc := Prj.DM_PhysicalDocuments(I);
            For J := 0 To Doc.DM_NetCount - 1 Do
            Begin
                Net := Doc.DM_Nets(J);
                If Net.DM_PinCount = 0 Then Continue;
                Lines.Add('(');
                Lines.Add(Net.DM_CalculatedNetName);
                For K := 0 To Net.DM_PinCount - 1 Do
                Begin
                    Pin := Net.DM_Pins(K);
                    Lines.Add(Pin.DM_PhysicalPartDesignator + '-' + Pin.DM_PinNumber);
                End;
                Lines.Add(')');
                Inc(NetCnt);
            End;
        End;

        OutDir := ExtractFilePath(Prj.DM_ProjectFileName);
        If OutDir = '' Then OutDir := ExtractFilePath(Prj.DM_ProjectFullPath);
        If OutDir = '' Then OutDir := GetCurrentDir + '\';
        OutPath := OutDir + ChangeFileExt(ExtractFileName(Prj.DM_ProjectFileName), '.net');
        Lines.SaveToFile(OutPath);
        ShowMessage('Protel netlist written (' + IntToStr(CompCnt) + ' components, '
                    + IntToStr(NetCnt) + ' nets):' + #13#10 + OutPath);
    Finally
        Lines.Free;
    End;
End;
