<#import "template.ftl" as layout>
<@layout.registrationLayout displayInfo=true; section>
    <#if section = "header">
        ${msg("emailVerifyTitle")}
    <#elseif section = "form">
        <p class="instruction">${msg("emailVerifyInstruction1",user.email)}</p>

        <div id="kc-form-options" class="${properties.kcFormGroupClass!}">
            <div class="${properties.kcFormOptionsWrapperClass!}">
                <span><a href="${url.loginRestartFlowUrl}">${kcSanitize(msg("backToLogin"))?no_esc}</a></span>
            </div>
        </div>

        <#if (client.baseUrl)?has_content>
        <div id="kc-form-options" class="${properties.kcFormGroupClass!}">
            <div class="${properties.kcFormOptionsWrapperClass!}">
                <span><a href="${client.baseUrl}">${kcSanitize(msg("backToApplication"))?no_esc}</a></span>
            </div>
        </div>
        </#if>
    <#elseif section = "info">
        <p class="instruction">
            ${msg("emailVerifyInstruction2")}
            <a href="${url.loginAction}">${msg("doClickHere")}</a> ${msg("emailVerifyInstruction3")}
        </p>
    </#if>
</@layout.registrationLayout>
