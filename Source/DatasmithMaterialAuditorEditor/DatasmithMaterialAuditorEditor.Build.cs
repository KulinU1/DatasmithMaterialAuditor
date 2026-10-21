using UnrealBuildTool;

public class DatasmithMaterialAuditorEditor : ModuleRules
{
    public DatasmithMaterialAuditorEditor(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

        PublicDependencyModuleNames.AddRange(
            new string[]
            {
                "Core",
                "CoreUObject",
                "Engine",
                "GeometryCore",
                "MeshDescription",
                "StaticMeshDescription"
            }
        );

        PrivateDependencyModuleNames.AddRange(
            new string[]
            {
                "UnrealEd"
            }
        );
    }
}
